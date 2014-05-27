# Copyright (c) 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
import unittest
from collections import defaultdict

from swift.common.middleware import keystoneauth
from swift.common.swob import Request, Response
from swift.common.http import HTTP_FORBIDDEN
from swiftpolicy.enforcer import AclCheck


class UnmockTimeModule(object):
    """
    Even if a test mocks time.time - you can restore unmolested behavior in a
    another module who imports time directly by monkey patching it's imported
    reference to the module with an instance of this class
    """

    _orig_time = time.time

    def __getattribute__(self, name):
        if name == 'time':
            return UnmockTimeModule._orig_time
        return getattr(time, name)


# logging.LogRecord.__init__ calls time.time
logging.time = UnmockTimeModule()


class FakeLogger(logging.Logger):
    # a thread safe logger

    def __init__(self, *args, **kwargs):
        self._clear()
        self.name = 'swift.unit.fake_logger'
        self.level = logging.NOTSET
        if 'facility' in kwargs:
            self.facility = kwargs['facility']
        self.statsd_client = None
        self.thread_locals = None

    def _clear(self):
        self.log_dict = defaultdict(list)
        self.lines_dict = defaultdict(list)

    def _store_in(store_name):
        def stub_fn(self, *args, **kwargs):
            self.log_dict[store_name].append((args, kwargs))
        return stub_fn

    def _store_and_log_in(store_name):
        def stub_fn(self, *args, **kwargs):
            self.log_dict[store_name].append((args, kwargs))
            self._log(store_name, args[0], args[1:], **kwargs)
        return stub_fn

    def get_lines_for_level(self, level):
        return self.lines_dict[level]

    error = _store_and_log_in('error')
    info = _store_and_log_in('info')
    warning = _store_and_log_in('warning')
    warn = _store_and_log_in('warning')
    debug = _store_and_log_in('debug')

    def exception(self, *args, **kwargs):
        self.log_dict['exception'].append((args, kwargs,
                                           str(sys.exc_info()[1])))
        print 'FakeLogger Exception: %s' % self.log_dict

    # mock out the StatsD logging methods:
    increment = _store_in('increment')
    decrement = _store_in('decrement')
    timing = _store_in('timing')
    timing_since = _store_in('timing_since')
    update_stats = _store_in('update_stats')
    set_statsd_prefix = _store_in('set_statsd_prefix')

    def get_increments(self):
        return [call[0][0] for call in self.log_dict['increment']]

    def get_increment_counts(self):
        counts = {}
        for metric in self.get_increments():
            if metric not in counts:
                counts[metric] = 0
            counts[metric] += 1
        return counts

    def setFormatter(self, obj):
        self.formatter = obj

    def close(self):
        self._clear()

    def set_name(self, name):
        # don't touch _handlers
        self._name = name

    def acquire(self):
        pass

    def release(self):
        pass

    def createLock(self):
        pass

    def emit(self, record):
        pass

    def _handle(self, record):
        try:
            line = record.getMessage()
        except TypeError:
            print 'WARNING: unable to format log message %r %% %r' % (
                record.msg, record.args)
            raise
        self.lines_dict[record.levelno].append(line)

    def handle(self, record):
        self._handle(record)

    def flush(self):
        pass

    def handleError(self, record):
        pass


class FakeApp(object):
    def __init__(self, status_headers_body_iter=None):
        self.calls = 0
        self.status_headers_body_iter = status_headers_body_iter
        if not self.status_headers_body_iter:
            self.status_headers_body_iter = iter([('404 Not Found', {}, '')])

    def __call__(self, env, start_response):
        self.calls += 1
        self.request = Request.blank('', environ=env)
        if 'swift.authorize' in env:
            resp = env['swift.authorize'](self.request)
            if resp:
                return resp(env, start_response)
        status, headers, body = self.status_headers_body_iter.next()
        return Response(status=status, headers=headers,
                        body=body)(env, start_response)


class SwiftAuth(unittest.TestCase):
    def setUp(self):
        self.test_auth = keystoneauth.filter_factory({})(FakeApp())
        self.test_auth.logger = FakeLogger()

    def _make_request(self, path=None, headers=None, **kwargs):
        if not path:
            path = '/v1/%s/c/o' % self.test_auth._get_account_for_tenant('foo')
        return Request.blank(path, headers=headers, **kwargs)

    def _get_identity_headers(self, status='Confirmed', tenant_id='1',
                              tenant_name='acct', user='usr', role=''):
        return dict(X_IDENTITY_STATUS=status,
                    X_TENANT_ID=tenant_id,
                    X_TENANT_NAME=tenant_name,
                    X_ROLES=role,
                    X_USER_NAME=user)

    def _get_successful_middleware(self):
        response_iter = iter([('200 OK', {}, '')])
        return keystoneauth.filter_factory({})(FakeApp(response_iter))

    def test_invalid_request_authorized(self):
        role = self.test_auth.reseller_admin_role
        headers = self._get_identity_headers(role=role)
        req = self._make_request('/', headers=headers)
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 404)

    def test_invalid_request_non_authorized(self):
        req = self._make_request('/')
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 404)

    def test_confirmed_identity_is_authorized(self):
        role = self.test_auth.reseller_admin_role
        headers = self._get_identity_headers(role=role)
        req = self._make_request('/v1/AUTH_acct/c', headers)
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 200)

    def test_detect_reseller_request(self):
        role = self.test_auth.reseller_admin_role
        headers = self._get_identity_headers(role=role)
        req = self._make_request('/v1/AUTH_acct/c', headers)
        req.get_response(self._get_successful_middleware())
        self.assertTrue(req.environ.get('reseller_request'))

    def test_confirmed_identity_is_not_authorized(self):
        headers = self._get_identity_headers()
        req = self._make_request('/v1/AUTH_acct/c', headers)
        resp = req.get_response(self.test_auth)
        self.assertEqual(resp.status_int, 403)

    def test_anonymous_is_authorized_for_permitted_referrer(self):
        req = self._make_request(headers={'X_IDENTITY_STATUS': 'Invalid'})
        req.acl = '.r:*'
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 200)

    def test_anonymous_with_validtoken_authorized_for_permitted_referrer(self):
        req = self._make_request(headers={'X_IDENTITY_STATUS': 'Confirmed'})
        req.acl = '.r:*'
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 200)

    def test_anonymous_is_not_authorized_for_unknown_reseller_prefix(self):
        req = self._make_request(path='/v1/BLAH_foo/c/o',
                                 headers={'X_IDENTITY_STATUS': 'Invalid'})
        # check user is not authorized even object is "public"
        req.acl = '.r:*'
        resp = req.get_response(self.test_auth)
        self.assertEqual(resp.status_int, 401)

    def test_blank_reseller_prefix(self):
        conf = {'reseller_prefix': ''}
        test_auth = keystoneauth.filter_factory(conf)(FakeApp())
        account = tenant_id = 'foo'
        self.assertEqual(account, test_auth._get_account_for_tenant(tenant_id))

    def test_reseller_prefix_added_underscore(self):
        conf = {'reseller_prefix': 'AUTH'}
        test_auth = keystoneauth.filter_factory(conf)(FakeApp())
        self.assertEqual(test_auth.reseller_prefix, "AUTH_")

    def test_reseller_prefix_not_added_double_underscores(self):
        conf = {'reseller_prefix': 'AUTH_'}
        test_auth = keystoneauth.filter_factory(conf)(FakeApp())
        self.assertEqual(test_auth.reseller_prefix, "AUTH_")

    def test_override_asked_for_but_not_allowed(self):
        conf = {'allow_overrides': 'false'}
        self.test_auth = keystoneauth.filter_factory(conf)(FakeApp())
        req = self._make_request('/v1/AUTH_account',
                                 environ={'swift.authorize_override': True})
        resp = req.get_response(self.test_auth)
        self.assertEquals(resp.status_int, 401)

    def test_override_asked_for_and_allowed(self):
        conf = {'allow_overrides': 'true'}
        self.test_auth = keystoneauth.filter_factory(conf)(FakeApp())
        req = self._make_request('/v1/AUTH_account',
                                 environ={'swift.authorize_override': True})
        resp = req.get_response(self.test_auth)
        self.assertEquals(resp.status_int, 404)

    def test_override_default_allowed(self):
        req = self._make_request('/v1/AUTH_account',
                                 environ={'swift.authorize_override': True})
        resp = req.get_response(self.test_auth)
        self.assertEquals(resp.status_int, 404)

    def test_anonymous_options_allowed(self):
        req = self._make_request('/v1/AUTH_account',
                                 environ={'REQUEST_METHOD': 'OPTIONS'})
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 200)

    def test_identified_options_allowed(self):
        headers = self._get_identity_headers()
        headers['REQUEST_METHOD'] = 'OPTIONS'
        req = self._make_request('/v1/AUTH_account',
                                 headers=self._get_identity_headers(),
                                 environ={'REQUEST_METHOD': 'OPTIONS'})
        resp = req.get_response(self._get_successful_middleware())
        self.assertEqual(resp.status_int, 200)

    def test_auth_scheme(self):
        req = self._make_request(path='/v1/BLAH_foo/c/o',
                                 headers={'X_IDENTITY_STATUS': 'Invalid'})
        resp = req.get_response(self.test_auth)
        self.assertEqual(resp.status_int, 401)
        self.assertTrue('Www-Authenticate' in resp.headers)


class TestAuthorize(unittest.TestCase):
    def setUp(self):
        self.test_auth = keystoneauth.filter_factory({})(FakeApp())
        self.test_auth.logger = FakeLogger()

    def _make_request(self, path, **kwargs):
        return Request.blank(path, **kwargs)

    def _get_account(self, identity=None):
        if not identity:
            identity = self._get_identity()
        return self.test_auth._get_account_for_tenant(
            identity['HTTP_X_TENANT_ID'])

    def _get_identity(self, tenant_id='tenant_id', tenant_name='tenant_name',
                      user_id='user_id', user_name='user_name', roles=[]):
        if isinstance(roles, list):
            roles = ','.join(roles)
        return {'HTTP_X_USER_ID': user_id,
                'HTTP_X_USER_NAME': user_name,
                'HTTP_X_TENANT_ID': tenant_id,
                'HTTP_X_TENANT_NAME': tenant_name,
                'HTTP_X_ROLES': roles,
                'HTTP_X_IDENTITY_STATUS': 'Confirmed'}

    def _check_authenticate(self, account=None, identity=None, headers=None,
                            exception=None, acl=None, env=None, path=None):
        if not identity:
            identity = self._get_identity()
        if not account:
            account = self._get_account(identity)
        if not path:
            path = '/v1/%s/c' % account
        default_env = {'REMOTE_USER': identity['HTTP_X_TENANT_ID']}
        default_env.update(identity)
        if env:
            default_env.update(env)
        req = self._make_request(path, headers=headers, environ=default_env)
        req.acl = acl
        result = self.test_auth.authorize(req)

        # if we have requested an exception but nothing came back then
        if exception and not result:
            self.fail("error %s was not returned" % (str(exception)))
        elif exception:
            self.assertEquals(result.status_int, exception)
        else:
            self.assertTrue(result is None)
        return req

    def test_authorize_fails_for_unauthorized_user(self):
        self._check_authenticate(exception=HTTP_FORBIDDEN)

    def test_authorize_fails_for_invalid_reseller_prefix(self):
        self._check_authenticate(account='BLAN_a',
                                 exception=HTTP_FORBIDDEN)

    def test_authorize_succeeds_for_reseller_admin(self):
        roles = [self.test_auth.reseller_admin_role]
        identity = self._get_identity(roles=roles)
        req = self._check_authenticate(identity=identity)
        self.assertTrue(req.environ.get('swift_owner'))

    def test_authorize_succeeds_for_insensitive_reseller_admin(self):
        roles = [self.test_auth.reseller_admin_role.upper()]
        identity = self._get_identity(roles=roles)
        req = self._check_authenticate(identity=identity)
        self.assertTrue(req.environ.get('swift_owner'))

    def test_authorize_succeeds_as_owner_for_operator_role(self):
        roles = self.test_auth.operator_roles.split(',')
        identity = self._get_identity(roles=roles)
        req = self._check_authenticate(identity=identity)
        self.assertTrue(req.environ.get('swift_owner'))

    def test_authorize_succeeds_as_owner_for_insensitive_operator_role(self):
        #import pdb; pdb.set_trace()
        roles = [r.upper() for r in self.test_auth.operator_roles.split(',')]
        identity = self._get_identity(roles=roles)
        req = self._check_authenticate(identity=identity)
        self.assertTrue(req.environ.get('swift_owner'))

    def _check_authorize_for_tenant_owner_match(self, exception=None):
        identity = self._get_identity(user_name='same_name',
                                      tenant_name='same_name')
        req = self._check_authenticate(identity=identity, exception=exception)
        expected = bool(exception is None)
        self.assertEqual(bool(req.environ.get('swift_owner')), expected)

    def test_authorize_succeeds_as_owner_for_tenant_owner_match(self):
        self.test_auth.is_admin = True
        self._check_authorize_for_tenant_owner_match()

    def test_authorize_fails_as_owner_for_tenant_owner_match(self):
        self.test_auth.is_admin = False
        self._check_authorize_for_tenant_owner_match(
            exception=HTTP_FORBIDDEN)

    def test_authorize_succeeds_for_container_sync(self):
        env = {'swift_sync_key': 'foo', 'REMOTE_ADDR': '127.0.0.1'}
        headers = {'x-container-sync-key': 'foo', 'x-timestamp': '1'}
        self._check_authenticate(env=env, headers=headers)

    def test_authorize_fails_for_invalid_referrer(self):
        env = {'HTTP_REFERER': 'http://invalid.com/index.html'}
        self._check_authenticate(acl='.r:example.com', env=env,
                                 exception=HTTP_FORBIDDEN)

    def test_authorize_fails_for_referrer_without_rlistings(self):
        env = {'HTTP_REFERER': 'http://example.com/index.html'}
        self._check_authenticate(acl='.r:example.com', env=env,
                                 exception=HTTP_FORBIDDEN)

    def test_authorize_succeeds_for_referrer_with_rlistings(self):
        env = {'HTTP_REFERER': 'http://example.com/index.html'}
        self._check_authenticate(acl='.r:example.com,.rlistings', env=env)

    def test_authorize_succeeds_for_referrer_with_obj(self):
        path = '/v1/%s/c/o' % self._get_account()
        env = {'HTTP_REFERER': 'http://example.com/index.html'}
        self._check_authenticate(acl='.r:example.com', env=env, path=path)

    def test_authorize_succeeds_for_user_role_in_roles(self):
        acl = 'allowme'
        identity = self._get_identity(roles=[acl])
        self._check_authenticate(identity=identity, acl=acl)

    def test_authorize_succeeds_for_tenant_name_user_in_roles(self):
        identity = self._get_identity()
        user_name = identity['HTTP_X_USER_NAME']
        user_id = identity['HTTP_X_USER_ID']
        tenant_id = identity['HTTP_X_TENANT_ID']
        for user in [user_id, user_name, '*']:
            acl = '%s:%s' % (tenant_id, user)
            self._check_authenticate(identity=identity, acl=acl)

    def test_authorize_succeeds_for_tenant_id_user_in_roles(self):
        identity = self._get_identity()
        user_name = identity['HTTP_X_USER_NAME']
        user_id = identity['HTTP_X_USER_ID']
        tenant_name = identity['HTTP_X_TENANT_NAME']
        for user in [user_id, user_name, '*']:
            acl = '%s:%s' % (tenant_name, user)
            self._check_authenticate(identity=identity, acl=acl)

    def test_authorize_succeeds_for_wildcard_tenant_user_in_roles(self):
        identity = self._get_identity()
        user_name = identity['HTTP_X_USER_NAME']
        user_id = identity['HTTP_X_USER_ID']
        for user in [user_id, user_name, '*']:
            acl = '*:%s' % user
            self._check_authenticate(identity=identity, acl=acl)

    def test_delete_own_account_not_allowed(self):
        roles = self.test_auth.operator_roles.split(',')
        identity = self._get_identity(roles=roles)
        account = self._get_account(identity)
        self._check_authenticate(account=account,
                                 identity=identity,
                                 exception=HTTP_FORBIDDEN,
                                 path='/v1/' + account,
                                 env={'REQUEST_METHOD': 'DELETE'})

    def test_delete_own_account_when_reseller_allowed(self):
        roles = [self.test_auth.reseller_admin_role]
        identity = self._get_identity(roles=roles)
        account = self._get_account(identity)
        req = self._check_authenticate(account=account,
                                       identity=identity,
                                       path='/v1/' + account,
                                       env={'REQUEST_METHOD': 'DELETE'})
        self.assertEqual(bool(req.environ.get('swift_owner')), True)


class TestAclCheckCrossTenant(unittest.TestCase):
    def setUp(self):
        self.cross_tenant_check = AclCheck._authorize_cross_tenant

    def test_cross_tenant_authorization_success(self):
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME',
                ['tenantID:userA']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME',
                ['tenantNAME:userA']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME', ['*:userA']),
            True)

        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME',
                ['tenantID:userID']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME',
                ['tenantNAME:userID']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME', ['*:userID']),
            True)

        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME', ['tenantID:*']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME', ['tenantNAME:*']),
            True)
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME', ['*:*']),
            True)

    def test_cross_tenant_authorization_failure(self):
        self.assertEqual(
            self.cross_tenant_check(
                'userID', 'userA', 'tenantID', 'tenantNAME',
                ['tenantXYZ:userA']),
            False)


if __name__ == '__main__':
    unittest.main()
