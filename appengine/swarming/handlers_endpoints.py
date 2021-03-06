# Copyright 2015 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""This module defines Swarming Server endpoints handlers."""

import datetime
import logging
import os

from google.appengine.api import datastore_errors
from google.appengine.api import memcache
from google.appengine.ext import ndb

import endpoints
import gae_ts_mon
from protorpc import messages
from protorpc import message_types
from protorpc import protojson
from protorpc import remote

from components import auth
from components import datastore_utils
from components import endpoints_webapp2
from components import machine_provider
from components import utils

import message_conversion
import swarming_rpcs
from server import acl
from server import bot_code
from server import bot_management
from server import config
from server import service_accounts
from server import task_pack
from server import task_queues
from server import task_request
from server import task_result
from server import task_scheduler


### Helper Methods


# Used by get_request_and_result(), clearer than using True/False and important
# as this is part of the security boundary.
_EDIT = object()
_VIEW = object()


# Add support for BooleanField in protorpc in endpoints GET requests.
_old_decode_field = protojson.ProtoJson.decode_field
def _decode_field(self, field, value):
  if (isinstance(field, messages.BooleanField) and
      isinstance(value, basestring)):
    return value.lower() == 'true'
  return _old_decode_field(self, field, value)
protojson.ProtoJson.decode_field = _decode_field


def get_request_and_result(task_id, viewing):
  """Provides the key and TaskRequest corresponding to a task ID.

  Enforces the ACL for users. Allows bots all access for the moment.

  Returns:
    tuple(TaskRequest, result): result can be either for a TaskRunResult or a
                                TaskResultSummay.
  """
  try:
    request_key, result_key = task_pack.get_request_and_result_keys(task_id)
    request, result = ndb.get_multi((request_key, result_key))
  except ValueError:
    raise endpoints.BadRequestException('%s is an invalid key.' % task_id)
  if not request or not result:
    raise endpoints.NotFoundException('%s not found.' % task_id)
  if viewing == _VIEW:
    if not acl.can_view_task(request):
      raise endpoints.ForbiddenException('%s is not accessible.' % task_id)
  elif viewing == _EDIT:
    if not acl.can_edit_task(request):
      raise endpoints.ForbiddenException('%s is not accessible.' % task_id)
  else:
    raise endpoints.InternalServerErrorException('get_request_and_result()')
  return request, result


def get_or_raise(key):
  """Returns an entity or raises an endpoints exception if it does not exist."""
  result = key.get()
  if not result:
    raise endpoints.NotFoundException('%s not found.' % key.id())
  return result


def apply_property_defaults(properties):
  """Fills ndb task properties with default values read from server settings."""
  cfg = config.settings()
  if not cfg:
    return

  cfg = config.settings()
  if cfg.isolate.default_server and cfg.isolate.default_namespace:
    properties.inputs_ref = properties.inputs_ref or task_request.FilesRef()
    properties.inputs_ref.isolatedserver = (
        properties.inputs_ref.isolatedserver or cfg.isolate.default_server)
    properties.inputs_ref.namespace = (
        properties.inputs_ref.namespace or cfg.isolate.default_namespace)

  if cfg.HasField('cipd') and properties.cipd_input:
    properties.cipd_input.server = (
        properties.cipd_input.server or cfg.cipd.default_server)
    properties.cipd_input.client_package = (
        properties.cipd_input.client_package or task_request.CipdPackage())
    properties.cipd_input.client_package.package_name = (
        properties.cipd_input.client_package.package_name or
        cfg.cipd.default_client_package.package_name)
    properties.cipd_input.client_package.version = (
        properties.cipd_input.client_package.version or
        cfg.cipd.default_client_package.version)


### API


swarming_api = auth.endpoints_api(
    name='swarming',
    version='v1',
    description=
        'API to interact with the Swarming service. Permits to create, '
        'view and cancel tasks, query tasks and bots')


VersionRequest = endpoints.ResourceContainer(
    message_types.VoidMessage,
    version=messages.IntegerField(1))


@swarming_api.api_class(resource_name='server', path='server')
class SwarmingServerService(remote.Service):
  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      message_types.VoidMessage, swarming_rpcs.ServerDetails,
      http_method='GET')
  @auth.require(acl.can_access)
  def details(self, _request):
    """Returns information about the server."""
    host = 'https://' + os.environ['HTTP_HOST']

    cfg = config.settings()

    mpp = ''
    if cfg.mp and cfg.mp.server:
      mpp = cfg.mp.server
    # as a fallback, try pulling from datastore
    if not mpp:
      mpp = machine_provider.MachineProviderConfiguration.get_instance_url()
    if mpp:
      mpp = mpp + '/leases/%s'

    return swarming_rpcs.ServerDetails(
        bot_version=bot_code.get_bot_version(host)[0],
        server_version=utils.get_app_version(),
        machine_provider_template=mpp,
        display_server_url_template=cfg.display_server_url_template,
        luci_config=config.config.config_service_hostname(),
        default_isolate_server=cfg.isolate.default_server,
        default_isolate_namespace=cfg.isolate.default_namespace)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      message_types.VoidMessage, swarming_rpcs.BootstrapToken)
  @auth.require(acl.can_create_bot)
  def token(self, _request):
    """Returns a token to bootstrap a new bot."""
    return swarming_rpcs.BootstrapToken(
        bootstrap_token = bot_code.generate_bootstrap_token(),
      )

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      message_types.VoidMessage, swarming_rpcs.ClientPermissions,
      http_method='GET')
  @auth.public
  def permissions(self, _request):
    """Returns the caller's permissions."""
    return swarming_rpcs.ClientPermissions(
        delete_bot=acl.can_delete_bot(),
        terminate_bot=acl.can_edit_bot(),
        get_configs=acl.can_view_config(),
        put_configs=acl.can_edit_config(),
        cancel_task=acl._is_user() or acl.is_ip_whitelisted_machine(),
        cancel_tasks=acl.can_edit_all_tasks(),
        get_bootstrap_token=acl.can_create_bot())

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      VersionRequest, swarming_rpcs.FileContent,
      http_method='GET')
  @auth.require(acl.can_view_config)
  def get_bootstrap(self, request):
    """Retrieves the current or a previous version of bootstrap.py.

    When the file is sourced via luci-config, the version parameter is ignored.
    Eventually the support for 'version' will be removed completely.
    """
    obj = bot_code.get_bootstrap('', '', request.version)
    if not obj:
      return swarming_rpcs.FileContent()
    return swarming_rpcs.FileContent(
        content=obj.content.decode('utf-8'),
        who=obj.who,
        when=obj.when,
        version=obj.version)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      VersionRequest, swarming_rpcs.FileContent,
      http_method='GET')
  @auth.require(acl.can_view_config)
  def get_bot_config(self, request):
    """Retrieves the current or a previous version of bot_config.py.

    When the file is sourced via luci-config, the version parameter is ignored.
    Eventually the support for 'version' will be removed completely.
    """
    obj = bot_code.get_bot_config(request.version)
    if not obj:
      return swarming_rpcs.FileContent()
    return swarming_rpcs.FileContent(
        content=obj.content.decode('utf-8'),
        who=obj.who,
        when=obj.when,
        version=obj.version)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.FileContentRequest, swarming_rpcs.FileContent)
  @auth.require(acl.can_edit_config)
  def put_bootstrap(self, request):
    """Stores a new version of bootstrap.py.

    Warning: if a file exists in luci-config, the file stored by this function
    is ignored. Uploads are not blocked in case the file is later deleted from
    luci-config.
    """
    key = bot_code.store_bootstrap(request.content.encode('utf-8'))
    obj = key.get()
    return swarming_rpcs.FileContent(
        who=obj.who.to_bytes() if obj.who else None,
        when=obj.created_ts,
        version=str(obj.version))

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.FileContentRequest, swarming_rpcs.FileContent)
  @auth.require(acl.can_edit_config)
  def put_bot_config(self, request):
    """Stores a new version of bot_config.py.

    Warning: if a file exists in luci-config, the file stored by this function
    is ignored. Uploads are not blocked in case the file is later deleted from
    luci-config.
    """
    host = 'https://' + os.environ['HTTP_HOST']
    key = bot_code.store_bot_config(host, request.content.encode('utf-8'))
    obj = key.get()
    return swarming_rpcs.FileContent(
        who=obj.who.to_bytes() if obj.who else None,
        when=obj.created_ts,
        version=str(obj.version))


TaskId = endpoints.ResourceContainer(
    message_types.VoidMessage,
    task_id=messages.StringField(1, required=True))


TaskIdWithPerf = endpoints.ResourceContainer(
    message_types.VoidMessage,
    task_id=messages.StringField(1, required=True),
    include_performance_stats=messages.BooleanField(2, default=False))


@swarming_api.api_class(resource_name='task', path='task')
class SwarmingTaskService(remote.Service):
  """Swarming's task-related API."""
  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      TaskIdWithPerf, swarming_rpcs.TaskResult,
      name='result',
      path='{task_id}/result',
      http_method='GET')
  @auth.require(acl.can_access)
  def result(self, request):
    """Reports the result of the task corresponding to a task ID.

    It can be a 'run' ID specifying a specific retry or a 'summary' ID hidding
    the fact that a task may have been retried transparently, when a bot reports
    BOT_DIED.

    A summary ID ends with '0', a run ID ends with '1' or '2'.
    """
    logging.debug('%s', request)
    _, result = get_request_and_result(request.task_id, _VIEW)
    return message_conversion.task_result_to_rpc(
        result, request.include_performance_stats)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      TaskId, swarming_rpcs.TaskRequest,
      name='request',
      path='{task_id}/request',
      http_method='GET')
  @auth.require(acl.can_access)
  def request(self, request):
    """Returns the task request corresponding to a task ID."""
    logging.debug('%s', request)
    request_obj, _ = get_request_and_result(request.task_id, _VIEW)
    return message_conversion.task_request_to_rpc(request_obj)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      TaskId, swarming_rpcs.CancelResponse,
      name='cancel',
      path='{task_id}/cancel')
  @auth.require(acl.can_access)
  def cancel(self, request):
    """Cancels a task.

    If a bot was running the task, the bot will forcibly cancel the task.
    """
    logging.debug('%s', request)
    request_obj, result = get_request_and_result(request.task_id, _EDIT)
    ok, was_running = task_scheduler.cancel_task(request_obj, result.key)
    return swarming_rpcs.CancelResponse(ok=ok, was_running=was_running)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      TaskId, swarming_rpcs.TaskOutput,
      name='stdout',
      path='{task_id}/stdout',
      http_method='GET')
  @auth.require(acl.can_access)
  def stdout(self, request):
    """Returns the output of the task corresponding to a task ID."""
    # TODO(maruel): Add streaming. Real streaming is not supported by AppEngine
    # v1.
    # TODO(maruel): Send as raw content instead of encoded. This is not
    # supported by cloud endpoints.
    logging.debug('%s', request)
    _, result = get_request_and_result(request.task_id, _VIEW)
    output = result.get_output()
    if output:
      output = output.decode('utf-8', 'replace')
    return swarming_rpcs.TaskOutput(output=output)


@swarming_api.api_class(resource_name='tasks', path='tasks')
class SwarmingTasksService(remote.Service):
  """Swarming's tasks-related API."""
  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.NewTaskRequest, swarming_rpcs.TaskRequestMetadata)
  @auth.require(acl.can_create_task)
  def new(self, request):
    """Creates a new task.

    The task will be enqueued in the tasks list and will be executed at the
    earliest opportunity by a bot that has at least the dimensions as described
    in the task request.
    """
    sb = (request.properties.secret_bytes
          if request.properties is not None else None)
    if sb is not None:
      request.properties.secret_bytes = "HIDDEN"
    logging.debug('%s', request)
    if sb is not None:
      request.properties.secret_bytes = sb

    try:
      request, secret_bytes = message_conversion.new_task_request_from_rpc(
          request, utils.utcnow())
      apply_property_defaults(request.properties)
      task_request.init_new_request(
          request, acl.can_schedule_high_priority_tasks(), secret_bytes)
    except (datastore_errors.BadValueError, TypeError, ValueError) as e:
      raise endpoints.BadRequestException(e.message)

    # Make sure the caller is actually allowed to schedule the task before
    # asking the token server for a service account token.
    task_scheduler.check_schedule_request_acl(request)

    # If request.service_account is an email, contact the token server to
    # generate "OAuth token grant" (or grab a cached one). By doing this we
    # check that the given service account usage is allowed by the token server
    # rules at the time the task is posted. This check is also performed later
    # (when running the task), when we get the actual OAuth access token.
    if service_accounts.is_service_account(request.service_account):
      if not service_accounts.has_token_server():
        raise endpoints.BadRequestException(
            'This Swarming server doesn\'t support task service accounts '
            'because Token Server URL is not configured')
      max_lifetime_secs = (
          request.expiration_secs +
          request.properties.execution_timeout_secs +
          request.properties.grace_period_secs)
      try:
        # Note: this raises AuthorizationError if the user is not allowed to use
        # the requested account or service_accounts.InternalError if something
        # unexpected happens.
        request.service_account_token = service_accounts.get_oauth_token_grant(
            service_account=request.service_account,
            validity_duration=datetime.timedelta(seconds=max_lifetime_secs))
      except service_accounts.InternalError as exc:
        raise endpoints.InternalServerErrorException(exc.message)

    try:
      result_summary = task_scheduler.schedule_request(request, secret_bytes)
    except (datastore_errors.BadValueError, TypeError, ValueError) as e:
      raise endpoints.BadRequestException(e.message)

    previous_result = None
    if result_summary.deduped_from:
      previous_result = message_conversion.task_result_to_rpc(
          result_summary, False)

    return swarming_rpcs.TaskRequestMetadata(
        request=message_conversion.task_request_to_rpc(request),
        task_id=task_pack.pack_result_summary_key(result_summary.key),
        task_result=previous_result)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.TasksRequest, swarming_rpcs.TaskList,
      http_method='GET')
  @auth.require(acl.can_view_all_tasks)
  def list(self, request):
    """Returns tasks results based on the filters.

    This endpoint is significantly slower than 'count'. Use 'count' when
    possible.
    """
    # TODO(maruel): Rename 'list' to 'results'.
    # TODO(maruel): Rename 'TaskList' to 'TaskResults'.
    logging.debug('%s', request)
    now = utils.utcnow()
    try:
      items, cursor = datastore_utils.fetch_page(
          self._query_from_request(request), request.limit, request.cursor)
    except ValueError as e:
      raise endpoints.BadRequestException(
          'Inappropriate filter for tasks/list: %s' % e)
    except datastore_errors.NeedIndexError as e:
      logging.error('%s', e)
      raise endpoints.BadRequestException(
          'Requires new index, ask admin to create one.')
    except datastore_errors.BadArgumentError as e:
      logging.error('%s', e)
      raise endpoints.BadRequestException(
          'This combination is unsupported, sorry.')
    return swarming_rpcs.TaskList(
        cursor=cursor,
        items=[
          message_conversion.task_result_to_rpc(
              i, request.include_performance_stats)
          for i in items
        ],
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.TasksRequest, swarming_rpcs.TaskRequests,
      http_method='GET')
  @auth.require(acl.can_view_all_tasks)
  def requests(self, request):
    """Returns tasks requests based on the filters.

    This endpoint is slightly slower than 'list'. Use 'list' or 'count' when
    possible.
    """
    logging.debug('%s', request)
    if request.include_performance_stats:
      raise endpoints.BadRequestException(
          'Can\'t set include_performance_stats for tasks/list')
    now = utils.utcnow()
    try:
      # Get the TaskResultSummary keys, then fetch the corresponding
      # TaskRequest entities.
      keys, cursor = datastore_utils.fetch_page(
          self._query_from_request(request),
          request.limit, request.cursor, keys_only=True)
      items = ndb.get_multi(
          task_pack.result_summary_key_to_request_key(k) for k in keys)
    except ValueError as e:
      raise endpoints.BadRequestException(
          'Inappropriate filter for tasks/requests: %s' % e)
    except datastore_errors.NeedIndexError as e:
      logging.error('%s', e)
      raise endpoints.BadRequestException(
          'Requires new index, ask admin to create one.')
    except datastore_errors.BadArgumentError as e:
      logging.error('%s', e)
      raise endpoints.BadRequestException(
          'This combination is unsupported, sorry.')
    return swarming_rpcs.TaskRequests(
        cursor=cursor,
        items=[message_conversion.task_request_to_rpc(i) for i in items],
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.TasksCancelRequest, swarming_rpcs.TasksCancelResponse,
      http_method='POST')
  @auth.require(acl.can_edit_all_tasks)
  def cancel(self, request):
    """Cancel a subset of pending tasks based on the tags.

    Cancellation happens asynchronously, so when this call returns,
    cancellations will not have completed yet.
    """
    logging.debug('%s', request)
    if not request.tags:
      # Prevent accidental cancellation of everything.
      raise endpoints.BadRequestException(
          'You must specify tags when cancelling multiple tasks.')

    now = utils.utcnow()
    query = task_result.TaskResultSummary.query(
        task_result.TaskResultSummary.state == task_result.State.PENDING)
    for tag in request.tags:
      query = query.filter(task_result.TaskResultSummary.tags == tag)

    tasks, cursor = datastore_utils.fetch_page(query, request.limit,
                                               request.cursor)

    if tasks:
      ok = utils.enqueue_task('/internal/taskqueue/cancel-tasks',
                              'cancel-tasks',
                              payload=','.join(t.task_id for t in tasks))
      if not ok:
        raise endpoints.InternalServerErrorException(
            'Could not enqueue cancel request, try again later')
    else:
      logging.info('No tasks to cancel.')

    return swarming_rpcs.TasksCancelResponse(
        cursor=cursor,
        matched=len(tasks),
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.TasksCountRequest, swarming_rpcs.TasksCount,
      http_method='GET')
  @auth.require(acl.can_view_all_tasks)
  def count(self, request):
    """Counts number of tasks in a given state."""
    logging.debug('%s', request)
    if not request.start:
      raise endpoints.BadRequestException('start (as epoch) is required')
    now = utils.utcnow()
    mem_key = self._memcache_key(request, now)
    count = memcache.get(mem_key, namespace='tasks_count')
    if count is not None:
      return swarming_rpcs.TasksCount(count=count, now=now)

    try:
      count = self._query_from_request(request, 'created_ts').count()
      memcache.add(mem_key, count, 24*60*60, namespace='tasks_count')
    except ValueError as e:
      raise endpoints.BadRequestException(
          'Inappropriate filter for tasks/count: %s' % e)
    return swarming_rpcs.TasksCount(count=count, now=now)

  def _memcache_key(self, request, now):
    # Floor now to minute to account for empty "end"
    end = request.end or now.replace(second=0, microsecond=0)
    request.tags.sort()
    return '%s|%s|%s|%s' % (request.tags, request.state, request.start, end)

  def _query_from_request(self, request, sort=None):
    """Returns a TaskResultSummary query."""
    start = message_conversion.epoch_to_datetime(request.start)
    end = message_conversion.epoch_to_datetime(request.end)
    return task_result.get_result_summaries_query(
        start, end,
        sort or request.sort.name.lower(),
        request.state.name.lower(),
        request.tags)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      message_types.VoidMessage, swarming_rpcs.TasksTags,
      http_method='GET')
  @auth.require(acl.can_view_all_tasks)
  def tags(self, _request):
    """Returns the cached set of tags currently seen in the fleet."""
    tags = task_result.TagAggregation.KEY.get()
    ft = [
      swarming_rpcs.StringListPair(key=t.tag, value=t.values)
      for t in tags.tags
    ]
    return swarming_rpcs.TasksTags(tasks_tags=ft, ts=tags.ts)


BotId = endpoints.ResourceContainer(
    message_types.VoidMessage,
    bot_id=messages.StringField(1, required=True))


BotEventsRequest = endpoints.ResourceContainer(
    swarming_rpcs.BotEventsRequest,
    bot_id=messages.StringField(1, required=True))


BotTasksRequest = endpoints.ResourceContainer(
    swarming_rpcs.BotTasksRequest,
    bot_id=messages.StringField(1, required=True))


@swarming_api.api_class(resource_name='bot', path='bot')
class SwarmingBotService(remote.Service):
  """Bot-related API. Permits querying information about the bot's properties"""
  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      BotId, swarming_rpcs.BotInfo,
      name='get',
      path='{bot_id}/get',
      http_method='GET')
  @auth.require(acl.can_view_bot)
  def get(self, request):
    """Returns information about a known bot.

    This includes its state and dimensions, and if it is currently running a
    task.
    """
    logging.debug('%s', request)
    bot_id = request.bot_id
    bot = bot_management.get_info_key(bot_id).get()
    deleted = False
    if not bot:
      # If there is not BotInfo, look if there are BotEvent child of this
      # entity. If this is the case, it means the bot was deleted but it's
      # useful to show information about it to the user even if the bot was
      # deleted. For example, it could be an machine-provider bot.
      events = bot_management.get_events_query(bot_id, True).fetch(1)
      if not events:
        raise endpoints.NotFoundException('%s not found.' % bot_id)
      bot = bot_management.BotInfo(
          key=bot_management.get_info_key(bot_id),
          dimensions_flat=bot_management.dimensions_to_flat(
              events[0].dimensions),
          state=events[0].state,
          external_ip=events[0].external_ip,
          authenticated_as=events[0].authenticated_as,
          version=events[0].version,
          quarantined=events[0].quarantined,
          task_id=events[0].task_id,
          last_seen_ts=events[0].ts,
          lease_id=events[0].lease_id,
          lease_expiration_ts=events[0].lease_expiration_ts,
          machine_type=events[0].machine_type)
      deleted = True

    return message_conversion.bot_info_to_rpc(bot, utils.utcnow(),
                                              deleted=deleted)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      BotId, swarming_rpcs.DeletedResponse,
      name='delete',
      path='{bot_id}/delete')
  @auth.require(acl.can_delete_bot)
  def delete(self, request):
    """Deletes the bot corresponding to a provided bot_id.

    At that point, the bot will not appears in the list of bots but it is still
    possible to get information about the bot with its bot id is known, as
    historical data is not deleted.

    It is meant to remove from the DB the presence of a bot that was retired,
    e.g. the VM was shut down already. Use 'terminate' instead of the bot is
    still alive.
    """
    logging.debug('%s', request)
    bot_key = bot_management.get_info_key(request.bot_id)
    get_or_raise(bot_key)  # raises 404 if there is no such bot
    # TODO(maruel): If the bot was a MP, call lease_management.cleanup_bot()?
    task_queues.cleanup_after_bot(request.bot_id)
    bot_key.delete()
    return swarming_rpcs.DeletedResponse(deleted=True)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      BotEventsRequest, swarming_rpcs.BotEvents,
      name='events',
      path='{bot_id}/events',
      http_method='GET')
  @auth.require(acl.can_view_bot)
  def events(self, request):
    """Returns events that happened on a bot."""
    logging.debug('%s', request)
    try:
      now = utils.utcnow()
      start = message_conversion.epoch_to_datetime(request.start)
      end = message_conversion.epoch_to_datetime(request.end)
      order = not (start or end)
      query = bot_management.get_events_query(request.bot_id, order)
      if not order:
        query = query.order(
            -bot_management.BotEvent.ts, bot_management.BotEvent.key)
      if start:
        query = query.filter(bot_management.BotEvent.ts >= start)
      if end:
        query = query.filter(bot_management.BotEvent.ts < end)
      items, cursor = datastore_utils.fetch_page(
          query, request.limit, request.cursor)
    except ValueError as e:
      raise endpoints.BadRequestException(
          'Inappropriate filter for bot.events: %s' % e)
    return swarming_rpcs.BotEvents(
        cursor=cursor,
        items=[message_conversion.bot_event_to_rpc(r) for r in items],
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      BotId, swarming_rpcs.TerminateResponse,
      name='terminate',
      path='{bot_id}/terminate')
  @auth.require(acl.can_edit_bot)
  def terminate(self, request):
    """Asks a bot to terminate itself gracefully.

    The bot will stay in the DB, use 'delete' to remove it from the DB
    afterward. This request returns a pseudo-taskid that can be waited for to
    wait for the bot to turn down.

    This command is particularly useful when a privileged user needs to safely
    debug a machine specific issue. The user can trigger a terminate for one of
    the bot exhibiting the issue, wait for the pseudo-task to run then access
    the machine with the guarantee that the bot is not running anymore.
    """
    # TODO(maruel): Disallow a terminate task when there's one currently
    # pending or if the bot is considered 'dead', e.g. no contact since 10
    # minutes.
    logging.debug('%s', request)
    bot_id = unicode(request.bot_id)
    bot_key = bot_management.get_info_key(bot_id)
    get_or_raise(bot_key)  # raises 404 if there is no such bot
    try:
      # Craft a special priority 0 task to tell the bot to shutdown.
      request = task_request.create_termination_task(
          bot_id, acl.can_schedule_high_priority_tasks())
    except (datastore_errors.BadValueError, TypeError, ValueError) as e:
      raise endpoints.BadRequestException(e.message)

    task_scheduler.check_schedule_request_acl(request)
    result_summary = task_scheduler.schedule_request(request, None)
    return swarming_rpcs.TerminateResponse(
        task_id=task_pack.pack_result_summary_key(result_summary.key))

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      BotTasksRequest, swarming_rpcs.BotTasks,
      name='tasks',
      path='{bot_id}/tasks',
      http_method='GET')
  @auth.require(acl.can_view_all_tasks)
  def tasks(self, request):
    """Lists a given bot's tasks within the specified date range.

    In this case, the tasks are effectively TaskRunResult since it's individual
    task tries sent to this specific bot.

    It is impossible to search by both tags and bot id. If there's a need,
    TaskRunResult.tags will be added (via a copy from TaskRequest.tags).
    """
    logging.debug('%s', request)
    try:
      start = message_conversion.epoch_to_datetime(request.start)
      end = message_conversion.epoch_to_datetime(request.end)
      now = utils.utcnow()
      query = task_result.get_run_results_query(
          start, end,
          request.sort.name.lower(),
          request.state.name.lower(),
          request.bot_id)
      items, cursor = datastore_utils.fetch_page(
          query, request.limit, request.cursor)
    except ValueError as e:
      raise endpoints.BadRequestException(
          'Inappropriate filter for bot.tasks: %s' % e)
    return swarming_rpcs.BotTasks(
        cursor=cursor,
        items=[
          message_conversion.task_result_to_rpc(
              r, request.include_performance_stats)
          for r in items
        ],
        now=now)


@swarming_api.api_class(resource_name='bots', path='bots')
class SwarmingBotsService(remote.Service):
  """Bots-related API."""

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.BotsRequest, swarming_rpcs.BotList,
      http_method='GET')
  @auth.require(acl.can_view_bot)
  def list(self, request):
    """Provides list of known bots.

    Deleted bots will not be listed.
    """
    logging.debug('%s', request)
    now = utils.utcnow()
    q = bot_management.BotInfo.query()
    try:
      q = bot_management.filter_dimensions(q, request.dimensions)
      q = bot_management.filter_availability(
          q, swarming_rpcs.to_bool(request.quarantined),
          swarming_rpcs.to_bool(request.is_dead), now,
          swarming_rpcs.to_bool(request.is_busy),
          swarming_rpcs.to_bool(request.is_mp))
    except ValueError as e:
      raise endpoints.BadRequestException(str(e))

    bots, cursor = datastore_utils.fetch_page(q, request.limit, request.cursor)
    return swarming_rpcs.BotList(
        cursor=cursor,
        death_timeout=config.settings().bot_death_timeout_secs,
        items=[message_conversion.bot_info_to_rpc(bot, now) for bot in bots],
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      swarming_rpcs.BotsRequest, swarming_rpcs.BotsCount,
      http_method='GET')
  @auth.require(acl.can_view_bot)
  def count(self, request):
    """Counts number of bots with given dimensions."""
    logging.debug('%s', request)
    now = utils.utcnow()
    q = bot_management.BotInfo.query()
    try:
      q = bot_management.filter_dimensions(q, request.dimensions)
    except ValueError as e:
      raise endpoints.BadRequestException(str(e))

    f_count = q.count_async()
    f_dead = (bot_management.filter_availability(q, None, True, now, None, None)
        .count_async())
    f_quarantined = (
        bot_management.filter_availability(q, True, None, now, None, None)
        .count_async())
    f_busy = (bot_management.filter_availability(q, None, None, now, True, None)
        .count_async())
    return swarming_rpcs.BotsCount(
        count=f_count.get_result(),
        quarantined=f_quarantined.get_result(),
        dead=f_dead.get_result(),
        busy=f_busy.get_result(),
        now=now)

  @gae_ts_mon.instrument_endpoint()
  @auth.endpoints_method(
      message_types.VoidMessage, swarming_rpcs.BotsDimensions,
      http_method='GET')
  @auth.require(acl.can_view_bot)
  def dimensions(self, _request):
    """Returns the cached set of dimensions currently in use in the fleet."""
    dims = bot_management.DimensionAggregation.KEY.get()
    fd = [
      swarming_rpcs.StringListPair(key=d.dimension, value=d.values)
      for d in dims.dimensions
    ]
    return swarming_rpcs.BotsDimensions(bots_dimensions=fd, ts=dims.ts)


def get_routes():
  return (
    endpoints_webapp2.api_routes(SwarmingServerService) +
    endpoints_webapp2.api_routes(SwarmingTaskService) +
    endpoints_webapp2.api_routes(SwarmingTasksService) +
    endpoints_webapp2.api_routes(SwarmingBotService) +
    endpoints_webapp2.api_routes(SwarmingBotsService) +
    # components.config endpoints for validation and configuring of luci-config
    # service URL.
    endpoints_webapp2.api_routes(config.ConfigApi))
