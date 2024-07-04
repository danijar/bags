import atexit
import collections
import functools
import multiprocessing as mp
import queue
import sys
import time
import traceback

import numpy as np
import cloudpickle

from . import utils


class Loader:

  def __init__(
      self, source, fns, batch, shuffle=True, prefetch=10,
      workers=128, shard_id=0, num_shards=1, seed=0):

    self.source = source
    self.fns = fns
    self.batch = batch
    self.shuffle = shuffle
    self.prefetch = prefetch
    self.shard_id = shard_id
    self.num_shards = num_shards
    self.seed = seed

    self.length = len(source)
    self.step = 0
    self.consumed = 0
    self.batches = collections.deque()
    self.futures = collections.deque()

    self.started = False
    self.stop = mp.Event()
    self.iqueue = mp.Queue()
    self.oqueue = mp.Queue()
    self.received = set()

    self.workers = []
    fns = cloudpickle.dumps(fns)
    args = (self.stop, self.iqueue, self.oqueue, source, fns, seed)
    for _ in range(workers):
      self.workers.append(mp.Process(target=self._worker, args=args))
    atexit.register(self.close)

  @functools.cached_property
  def spec(self):
    datapoint = self.source[0]
    datapoint = {k: np.asarray(v) for k, v in datapoint.items()}
    for fn in self.fns:
      datapoint = fn(datapoint, seed=[0, 0])
      assert isinstance(datapoint, dict), fn
    return {k: (v.dtype, v.shape) for k, v in datapoint.items()}

  def __iter__(self):
    self.started = True
    for _ in range(self.prefetch):
      self._request()
    [x.start() for x in self.workers]
    return self

  def __next__(self):
    assert self.started
    try:
      self._request()
      batch = self._receive()
      self.consumed += self.batch * self.num_shards
    except (SystemExit, KeyboardInterrupt):
      self.close()
      raise
    return batch

  def save(self):
    return {'step': self.consumed, 'seed': self.seed}

  def load(self, d):
    self.consumed = self.step = d['step']
    self.seed = d['seed']

  def close(self):
    self.stop.set()
    time.sleep(0.2)
    [x.join(timeout=0) for x in self.workers]
    for worker in self.workers:
      if worker.is_alive():
        worker.terminate()
    time.sleep(0.1)
    [x.join(timeout=0) for x in self.workers]
    for q in (self.iqueue, self.oqueue):
      q.close()
      q.cancel_join_thread()
      q.join_thread()
    self.batches.clear()

  @classmethod
  def _worker(cls, stop, iqueue, oqueue, source, fns, seed):
    try:
      fns = cloudpickle.loads(fns)
      while not stop.is_set():
        try:
          job = iqueue.get(timeout=0.1)
        except queue.Empty:
          continue
        index, step, batchdesc, loc = job
        batch = {k: utils.SharedArray(*v) for k, v in batchdesc.items()}
        datapoint = source[index]
        datapoint = {k: np.asarray(v) for k, v in datapoint.items()}
        for fn in fns:
          datapoint = fn(datapoint, seed=[seed, step])
          assert isinstance(datapoint, dict), fn
        assert datapoint.keys() == batch.keys()
        for key, value in datapoint.items():
          batch[key][loc] = value
        oqueue.put(step)
    except (SystemExit, KeyboardInterrupt):
      stop.set()
    except Exception:
      stop.set()
      oqueue.put(''.join(traceback.format_exception(sys.exception())))

  def _request(self):
    batch = {
        k: utils.SharedArray(d, (self.batch, *s))
        for k, (d, s) in self.spec.items()}
    batchdesc = {k: v.desc for k, v in batch.items()}
    self.batches.append(batch)
    for loc in range(self.batch):
      epoch = self.step // self.length
      index = self._order(epoch)[self.step % self.length]
      self.iqueue.put((index, self.step, batchdesc, loc))
      self.step += self.num_shards

  def _receive(self):
    collected = 0
    while collected < self.batch:
      try:
        result = self.oqueue.get(timeout=0.1)
      except queue.Empty:
        continue
      if not isinstance(result, int):
        self.close()
        raise RuntimeError(result)
      self.received.add(result)
      needed = self.consumed + collected
      if needed in self.received:
        self.received.remove(needed)
        collected += 1
    return self.batches.popleft()

  @functools.lru_cache(maxsize=1)
  def _order(self, epoch):
    if self.shuffle:
      rng = np.random.default_rng(seed=[self.seed, epoch])
      return rng.permutation(np.arange(self.length)).tolist()
    else:
      return list(range(self.length))
