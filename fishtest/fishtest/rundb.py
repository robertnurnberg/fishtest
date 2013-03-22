import copy
import os
from datetime import datetime
from bson.objectid import ObjectId
from pymongo import MongoClient, ASCENDING, DESCENDING

from userdb import UserDb

class RunDb:
  def __init__(self, db_name='fishtest_new'):
    # MongoDB server is assumed to be on the same machine, if not user should use
    # ssh with port forwarding to access the remote host.
    self.conn = MongoClient(os.getenv('FISHTEST_HOST') or 'localhost')
    self.db = self.conn[db_name]
    self.userdb = UserDb(self.db)
    self.runs = self.db['runs']

    self.chunk_size = 1000

  def generate_tasks(self, num_games):
    tasks = []
    remaining = num_games
    while remaining > 0:
      task_size = min(self.chunk_size, remaining)
      tasks.append({
        'num_games': task_size,
        'pending': True,
        'active': False,
      })
      remaining -= task_size
    return tasks

  def new_run(self, base_tag, new_tag, num_games, tc, book, book_depth, threads,
              name='',
              info='',
              resolved_base='',
              resolved_new='',
              base_signature='',
              new_signature='',
              start_time=None,
              username=None):
    if start_time == None:
      start_time = datetime.utcnow()

    id = self.runs.insert({
      'args': {
        'base_tag': base_tag,
        'new_tag': new_tag,
        'num_games': int(num_games),
        'tc': tc,
        'book': book,
        'book_depth': book_depth,
        'threads': int(threads),
        'resolved_base': resolved_base,
        'resolved_new': resolved_new,
        'name': name,
        'info': info,
        'base_signature': base_signature,
        'new_signature': new_signature,
        'username': username,
      },
      'start_time': start_time,
      # Will be filled in by tasks, indexed by task-id
      'tasks': self.generate_tasks(int(num_games)),
      # Aggregated results
      'results': { 'wins': 0, 'losses': 0, 'draws': 0 },
      'results_stale': False,
    })

    return id

  def get_machines(self):
    machines = []
    for run in self.runs.find({'tasks': {'$elemMatch': {'active': True}}}):
      for task in run['tasks']:
        if task['active']:
          machine = copy.copy(task['worker_info'])
          machine['last_updated'] = task.get('last_updated', None)
          machines.append(machine)
    return machines

  def get_run(self, id):
    return self.runs.find_one({'_id': ObjectId(id)})

  def get_runs(self, skip=0, limit=0):
    runs = []
    for run in self.runs.find(skip=skip, limit=limit, sort=[('start_time', DESCENDING)]):
      runs.append(run)
    return runs

  def get_results(self, run):
    if not run['results_stale']:
      return run['results']

    results = { 'wins': 0, 'losses': 0, 'draws': 0, 'crashes': 0 }
    for task in run['tasks']:
      if 'stats' in task:
        stats = task['stats']
        results['wins'] += stats['wins']
        results['losses'] += stats['losses']
        results['draws'] += stats['draws']
        results['crashes'] += stats.get('crashes', 0) # TODO remove get() when all worker will report crashes

    run['results_stale'] = False
    run['results'] = results
    self.runs.save(run)

    return results

  def request_task(self, worker_info):
    # Does this worker have a task already?  If so, just hand that back
    existing_run = self.runs.find_one({'tasks': {'$elemMatch': {'active': True, 'worker_info': worker_info}}})
    if existing_run != None:
      for task_id, task in enumerate(existing_run['tasks']):
        if task['active'] and task['worker_info'] == worker_info:
          if task['pending']:
            return {'run': existing_run, 'task_id': task_id}
          else:
            # Don't hand back tasks that have been marked as no longer pending
            task['active'] = False
            self.runs.save(existing_run)

    # Ok, we get a new task that does not require more threads than available concurrency
    max_threads = int(worker_info['concurrency'])
    q = {
      'new': True,
      'query': { '$and': [ {'tasks': {'$elemMatch': {'active': False, 'pending': True}}},
                           { '$or': [ {'args.$.threads': { '$exists': False }},
                                      {'args.$.threads': { '$lte': max_threads }}]}]},
      'sort': [('_id', ASCENDING)],
      'update': {
        '$set': {
          'tasks.$.active': True,
          'tasks.$.last_updated': datetime.utcnow(),
          'tasks.$.worker_info': worker_info,
        }
      }
    }

    run = self.runs.find_and_modify(**q)
    if run == None:
      return {'task_waiting': False}

    latest_time = datetime.min
    for idx, task in enumerate(run['tasks']):
      if 'last_updated' in task and task['last_updated'] > latest_time:
        latest_time = task['last_updated']
        task_id = idx

    return {'run': run, 'task_id': task_id}

  def update_task(self, run_id, task_id, stats):
    run = self.get_run(run_id)
    task = run['tasks'][task_id]
    if not task['active']:
      return {'task_alive': False}

    task_alive = task['pending']

    task['stats'] = stats
    if stats['wins'] + stats['losses'] + stats['draws'] >= task['num_games']:
      task['active'] = False
      task['pending'] = False

    update_time = datetime.utcnow()
    task['last_updated'] = update_time
    run['last_updated'] = update_time
    run['results_stale'] = True
    self.runs.save(run)

    return {'task_alive': task_alive}

  def failed_task(self, run_id, task_id):
    run = self.get_run(run_id)
    task = run['tasks'][task_id]
    if not task['active']:
      # TODO: log error?
      return

    # Mark the task as inactive: it will be rescheduled
    task['active'] = False
    self.runs.save(run)

    return {}
