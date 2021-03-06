#!/usr/bin/env python
# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import logging
import sys
import threading
import time
import unittest

import test_env_bot_code
test_env_bot_code.setup_test_env()

from depot_tools import auto_stub
import remote_client_grpc


class FakeGrpcProxy(object):
  def __init__(self, testobj):
    self._testobj = testobj

  @property
  def prefix(self):
    return 'inst'

  def call_unary(self, name, request):
    return self._testobj._handle_call(name, request)


class TestRemoteClientGrpc(auto_stub.TestCase):
  def setUp(self):
    super(TestRemoteClientGrpc, self).setUp()
    self._num_sleeps = 0
    def fake_sleep(_time):
      self._num_sleeps += 1
    self.mock(time, 'sleep', fake_sleep)
    self._client = remote_client_grpc.RemoteClientGrpc('', FakeGrpcProxy(self))
    self._expected = []
    self._error_codes = []

  def _handle_call(self, method, request):
    """This is called by FakeGrpcProxy to implement fake calls"""
    # Pop off the first item on the list
    self.assertTrue(len(self._expected) > 0)
    expected, self._expected = self._expected[0], self._expected[1:]
    # Each element of the "expected" array should be a 3-tuple:
    #    * The name of the method (eg 'TaskUpdate')
    #    * The proto request
    #    * The proto response
    self.assertEqual(method, expected[0])
    self.assertEqual(str(request), str(expected[1]))
    return expected[2]

  def test_handshake(self):
    """Tests the handshake proto"""
    attrs = {
        'version': '123',
        'dimensions': {
            'id': ['robocop'],
            'pool': ['swimming'],
            'mammal': ['ferrett', 'wombat'],
        },
        'state': {},
    }

    msg_req = remote_client_grpc.bots_pb2.CreateBotSessionRequest()
    msg_req.parent = 'inst'
    session = msg_req.bot_session
    session.bot_id = 'robocop'
    session.health = remote_client_grpc.bots_pb2.HEALTHY
    session.version = '123'
    worker = session.worker
    wp = worker.properties.add()
    wp.key = 'pool'
    wp.value = 'swimming'
    dev = worker.devices.add()
    dev.handle = 'robocop'
    dp1 = dev.properties.add()
    dp1.key = 'mammal'
    dp1.value = 'ferrett'
    dp2 = dev.properties.add()
    dp2.key = 'mammal'
    dp2.value = 'wombat'

    # Create proto response, overriding the pool
    msg_rsp = remote_client_grpc.bots_pb2.BotSession()
    msg_rsp.CopyFrom(msg_req.bot_session)
    msg_rsp.worker.properties[0].value = 'dead'

    # Execute call and verify response
    expected_call = ('CreateBotSession', msg_req, msg_rsp)
    self._expected.append(expected_call)
    response = self._client.do_handshake(attrs)
    self.assertEqual(response, {
        'bot_version': u'123',
        'bot_group_cfg': {
            'dimensions': {
                u'pool': [u'dead'],
            },
        },
        'bot_group_cfg_version': 1,
    })


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.CRITICAL)
  unittest.TestCase.maxDiff = None
  unittest.main()
