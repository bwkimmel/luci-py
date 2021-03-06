# Copyright 2013 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Main entry point for Swarming service.

This file contains the URL handlers for all the Swarming service URLs,
implemented using the webapp2 framework.
"""

import collections
import os

import webapp2

import handlers_bot
import handlers_endpoints
import mapreduce_jobs
import template
from components import auth
from components import utils
from server import acl
from server import bot_code
from server import config


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


# Helper class for displaying the sort options in html templates.
SortOptions = collections.namedtuple('SortOptions', ['key', 'name'])


### is_admin pages.


class RestrictedConfigHandler(auth.AuthenticatingHandler):
  @auth.autologin
  @auth.require(acl.can_view_config)
  def get(self):
    # Template parameters schema matches settings_info() return value.
    self.response.write(template.render(
        'swarming/restricted_config.html', config.settings_info()))


class UploadBotConfigHandler(auth.AuthenticatingHandler):
  """Stores a new bot_config.py script."""

  @auth.autologin
  @auth.require(acl.can_view_config)
  def get(self):
    bot_config = bot_code.get_bot_config()
    params = {
      'content': bot_config.content.decode('utf-8'),
      'path': self.request.path,
      'version': bot_config.version,
      'when': bot_config.when,
      'who': bot_config.who or 'N/A',
      'xsrf_token': self.generate_xsrf_token(),
    }
    self.response.write(
        template.render('swarming/restricted_upload_bot_config.html', params))

  @auth.require(acl.can_edit_config)
  def post(self):
    script = self.request.get('script', '')
    if not script:
      self.abort(400, 'No script uploaded')

    # Make sure the script is valid utf-8. For some odd reason, the script
    # instead may or may not be an unicode instance. This depends if it is on
    # AppEngine production or not.
    if isinstance(script, str):
      script = script.decode('utf-8', 'replace')
    script = script.encode('utf-8')
    bot_code.store_bot_config(self.request.host_url, script)
    self.get()


class UploadBootstrapHandler(auth.AuthenticatingHandler):
  """Stores a new bootstrap.py script."""

  @auth.autologin
  @auth.require(acl.can_view_config)
  def get(self):
    bootstrap = bot_code.get_bootstrap(self.request.host_url)
    params = {
      'content': bootstrap.content.decode('utf-8'),
      'path': self.request.path,
      'version': bootstrap.version,
      'when': bootstrap.when,
      'who': bootstrap.who or 'N/A',
      'xsrf_token': self.generate_xsrf_token(),
    }
    self.response.write(
        template.render('swarming/restricted_upload_bootstrap.html', params))

  @auth.require(acl.can_edit_config)
  def post(self):
    script = self.request.get('script', '')
    if not script:
      self.abort(400, 'No script uploaded')

    # Make sure the script is valid utf-8. For some odd reason, the script
    # instead may or may not be an unicode instance. This depends if it is on
    # AppEngine production or not.
    if isinstance(script, str):
      script = script.decode('utf-8', 'replace')
    script = script.encode('utf-8')
    bot_code.store_bootstrap(script)
    self.get()


### Mapreduce related handlers


class RestrictedLaunchMapReduceJob(auth.AuthenticatingHandler):
  """Enqueues a task to start a map reduce job on the backend module.

  A tree of map reduce jobs inherits module and version of a handler that
  launched it. All UI handlers are executes by 'default' module. So to run a
  map reduce on a backend module one needs to pass a request to a task running
  on backend module.
  """

  @auth.require(acl.can_edit_config)
  def post(self):
    job_id = self.request.get('job_id')
    assert job_id in mapreduce_jobs.MAPREDUCE_JOBS
    success = utils.enqueue_task(
        url='/internal/taskqueue/mapreduce/launch/%s' % job_id,
        queue_name=mapreduce_jobs.MAPREDUCE_TASK_QUEUE,
        use_dedicated_module=False)
    # New tasks should show up on the status page.
    if success:
      self.redirect('/restricted/mapreduce/status')
    else:
      self.abort(500, 'Failed to launch the job')


### Redirectors.


class BotsListHandler(auth.AuthenticatingHandler):
  """Redirects to a list of known bots."""

  @auth.public
  def get(self):
    limit = int(self.request.get('limit', 100))

    dimensions = (
      l.strip() for l in self.request.get('dimensions', '').splitlines()
    )
    dimensions = [i for i in dimensions if i]

    new_ui_link = '/botlist?l=%d' % limit
    if dimensions:
      new_ui_link += '&f=' + '&f='.join(dimensions)

    self.redirect(new_ui_link)


class BotHandler(auth.AuthenticatingHandler):
  """Redirects to a page about the bot, including last tasks and events."""

  @auth.public
  def get(self, bot_id):
    self.redirect('/bot?id=%s' % bot_id)


class TasksHandler(auth.AuthenticatingHandler):
  """Redirects to a list of all task requests."""

  @auth.public
  def get(self):
    limit = int(self.request.get('limit', 100))
    task_tags = [
      line for line in self.request.get('task_tag', '').splitlines() if line
    ]

    new_ui_link = '/tasklist?l=%d' % limit
    if task_tags:
      new_ui_link += '&f=' + '&f='.join(task_tags)

    self.redirect(new_ui_link)


class TaskHandler(auth.AuthenticatingHandler):
  """Redirects to a page containing task request and result."""

  @auth.public
  def get(self, task_id):
    self.redirect('/task?id=%s' % task_id)


### Public pages.


class UIHandler(auth.AuthenticatingHandler):
  """Serves the landing page for the new UI of the requested page.

  This landing page is stamped with the OAuth 2.0 client id from the
  configuration."""
  @auth.public
  def get(self, page):
    if not page:
      page = 'swarming'

    params = {
      'client_id': config.settings().ui_client_id,
    }
    # Can cache for 1 week, because the only thing that would change in this
    # template is the oauth client id, which changes very infrequently.
    self.response.cache_control.no_cache = None
    self.response.cache_control.public = True
    self.response.cache_control.max_age = 604800
    try:
      self.response.write(template.render(
        'swarming/public_%s_index.html' % page, params))
    except template.TemplateNotFound:
      self.abort(404, 'Page not found.')

  def get_content_security_policy(self):
    # We use iframes to display pages at display_server_url_template. Need to
    # allow it in CSP.
    csp = super(UIHandler, self).get_content_security_policy()
    tmpl = config.settings().display_server_url_template
    if tmpl:
      if tmpl.startswith('/'):
        csp['child-src'].append("'self'")
      else:
        # We assume the template specifies '%s' in its last path component.
        # We strip it to get a "parent" path that we can put into CSP. Note that
        # whitelisting an entire display server domain is unnecessary wide.
        assert tmpl.startswith('https://'), tmpl
        csp['child-src'].append(tmpl[:tmpl.rfind('/')+1])
    return csp


class WarmupHandler(webapp2.RequestHandler):
  def get(self):
    auth.warmup()
    bot_code.get_swarming_bot_zip(self.request.host_url)
    utils.get_module_version_list(None, None)
    self.response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    self.response.write('ok')


class EmailHandler(webapp2.RequestHandler):
  """Blackhole any email sent."""
  def post(self, to):
    pass


def get_routes():
  routes = [
      ('/_ah/mail/<to:.+>', EmailHandler),
      ('/_ah/warmup', WarmupHandler),
  ]

  if not utils.should_disable_ui_routes():
    routes.extend([
      # Frontend pages. They return HTML.
      # Public pages.
      ('/<page:(bot|botlist|task|tasklist|)>', UIHandler),

      # Redirects to Polymer UI
      ('/user/tasks', TasksHandler),
      ('/user/task/<task_id:[0-9a-fA-F]+>', TaskHandler),
      ('/restricted/bots', BotsListHandler),
      ('/restricted/bot/<bot_id:[^/]+>', BotHandler),

      # Admin pages.
      # TODO(maruel): Get rid of them.
      ('/restricted/config', RestrictedConfigHandler),
      ('/restricted/upload/bot_config', UploadBotConfigHandler),
      ('/restricted/upload/bootstrap', UploadBootstrapHandler),

      # Mapreduce related urls.
      (r'/restricted/launch_mapreduce', RestrictedLaunchMapReduceJob),
    ])

  return [webapp2.Route(*i) for i in routes]


def create_application(debug):
  routes = []
  routes.extend(get_routes())
  routes.extend(handlers_bot.get_routes())
  routes.extend(handlers_endpoints.get_routes())
  return webapp2.WSGIApplication(routes, debug=debug)
