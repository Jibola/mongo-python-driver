# Copyright 2019-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test client side encryption spec."""

import base64
import copy
import os
import socket
import traceback
import sys
import uuid

sys.path[0:0] = [""]

from bson import BSON, json_util
from bson.binary import STANDARD, Binary, UUID_SUBTYPE
from bson.codec_options import CodecOptions
from bson.errors import BSONError
from bson.json_util import JSONOptions
from bson.raw_bson import RawBSONDocument
from bson.son import SON

from pymongo.errors import (ConfigurationError,
                            EncryptionError,
                            OperationFailure)
from pymongo.encryption_options import AutoEncryptionOpts, _HAVE_PYMONGOCRYPT
from pymongo.mongo_client import MongoClient
from pymongo.write_concern import WriteConcern

from test import unittest, IntegrationTest, PyMongoTestCase, client_context
from test.utils import (TestCreator,
                        camel_to_snake_args,
                        rs_or_single_client,
                        wait_until)
from test.utils_spec_runner import SpecRunner


if _HAVE_PYMONGOCRYPT:
    # Load the mongocrypt library.
    from pymongocrypt.binding import init
    init(os.environ.get('MONGOCRYPT_LIB', 'mongocrypt'))

# This has to be imported after calling init().
from pymongo.encryption import (Algorithm,
                                ClientEncryption)


def get_client_opts(client):
    return client._MongoClient__options


KMS_PROVIDERS = {'local': {'key': b'\x00'*96}}


class TestAutoEncryptionOpts(PyMongoTestCase):
    @unittest.skipIf(_HAVE_PYMONGOCRYPT, 'pymongocrypt is installed')
    def test_init_requires_pymongocrypt(self):
        with self.assertRaises(ConfigurationError):
            AutoEncryptionOpts({}, 'admin.datakeys')

    @unittest.skipUnless(_HAVE_PYMONGOCRYPT, 'pymongocrypt is not installed')
    def test_init(self):
        opts = AutoEncryptionOpts({}, 'admin.datakeys')
        self.assertEqual(opts._kms_providers, {})
        self.assertEqual(opts._key_vault_namespace, 'admin.datakeys')
        self.assertEqual(opts._key_vault_client, None)
        self.assertEqual(opts._schema_map, None)
        self.assertEqual(opts._bypass_auto_encryption, False)

        if hasattr(socket, 'AF_UNIX'):
            self.assertEqual(
                opts._mongocryptd_uri, 'mongodb://%2Ftmp%2Fmongocryptd.sock')
        else:
            self.assertEqual(
                opts._mongocryptd_uri, 'mongodb://localhost:27020')

        self.assertEqual(opts._mongocryptd_bypass_spawn, False)
        self.assertEqual(opts._mongocryptd_spawn_path, 'mongocryptd')
        self.assertEqual(
            opts._mongocryptd_spawn_args, ['--idleShutdownTimeoutSecs=60'])

    @unittest.skipUnless(_HAVE_PYMONGOCRYPT, 'pymongocrypt is not installed')
    def test_init_spawn_args(self):
        # User can override idleShutdownTimeoutSecs
        opts = AutoEncryptionOpts(
            {}, 'admin.datakeys',
            mongocryptd_spawn_args=['--idleShutdownTimeoutSecs=88'])
        self.assertEqual(
            opts._mongocryptd_spawn_args, ['--idleShutdownTimeoutSecs=88'])

        # idleShutdownTimeoutSecs is added by default
        opts = AutoEncryptionOpts(
            {}, 'admin.datakeys', mongocryptd_spawn_args=[])
        self.assertEqual(
            opts._mongocryptd_spawn_args, ['--idleShutdownTimeoutSecs=60'])

        # Also added when other options are given
        opts = AutoEncryptionOpts(
            {}, 'admin.datakeys',
            mongocryptd_spawn_args=['--quiet', '--port=27020'])
        self.assertEqual(
            opts._mongocryptd_spawn_args,
            ['--quiet', '--port=27020', '--idleShutdownTimeoutSecs=60'])


class TestClientOptions(PyMongoTestCase):
    def test_default(self):
        client = MongoClient(connect=False)
        self.addCleanup(client.close)
        self.assertEqual(get_client_opts(client).auto_encryption_opts, None)

        client = MongoClient(auto_encryption_opts=None, connect=False)
        self.addCleanup(client.close)
        self.assertEqual(get_client_opts(client).auto_encryption_opts, None)

    @unittest.skipUnless(_HAVE_PYMONGOCRYPT, 'pymongocrypt is not installed')
    def test_kwargs(self):
        opts = AutoEncryptionOpts(KMS_PROVIDERS, 'admin.datakeys')
        client = MongoClient(auto_encryption_opts=opts, connect=False)
        self.addCleanup(client.close)
        self.assertEqual(get_client_opts(client).auto_encryption_opts, opts)


class EncryptionIntegrationTest(IntegrationTest):
    """Base class for encryption integration tests."""

    @classmethod
    @unittest.skipUnless(_HAVE_PYMONGOCRYPT, 'pymongocrypt is not installed')
    @client_context.require_version_min(4, 2, -1)
    def setUpClass(cls):
        super(EncryptionIntegrationTest, cls).setUpClass()

    def assertEncrypted(self, val):
        self.assertIsInstance(val, Binary)
        self.assertEqual(val.subtype, 6)


# Location of JSON test files.
BASE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'client-side-encryption')
SPEC_PATH = os.path.join(BASE, 'spec')

OPTS = CodecOptions(uuid_representation=STANDARD)

# Use SON to preserve the order of fields while parsing json. Use tz_aware
# =False to match how CodecOptions decodes dates.
JSON_OPTS = JSONOptions(document_class=SON, uuid_representation=STANDARD,
                        tz_aware=False)


def read(*paths):
    with open(os.path.join(BASE, *paths)) as fp:
        return fp.read()


def json_data(*paths):
    return json_util.loads(read(*paths), json_options=JSON_OPTS)


def bson_data(*paths):
    return BSON.encode(json_data(*paths), codec_options=OPTS)


class TestClientSimple(EncryptionIntegrationTest):

    def _test_auto_encrypt(self, opts):
        client = rs_or_single_client(auto_encryption_opts=opts)
        self.addCleanup(client.close)

        # Create the encrypted field's data key.
        key_vault = self.client.admin.get_collection(
            'datakeys', codec_options=OPTS)
        data_key = RawBSONDocument(
            bson_data('custom', 'key-document-local.json'))
        key_vault.insert_one(data_key)
        self.addCleanup(key_vault.drop)

        # Collection.insert_one/insert_many auto encrypts.
        docs = [{'_id': 0, 'ssn': '000'},
                {'_id': 1, 'ssn': '111'},
                {'_id': 2, 'ssn': '222'},
                {'_id': 3, 'ssn': '333'},
                {'_id': 4, 'ssn': '444'},
                {'_id': 5, 'ssn': '555'}]
        encrypted_coll = client.pymongo_test.test
        encrypted_coll.insert_one(docs[0])
        encrypted_coll.insert_many(docs[1:3])
        unack = encrypted_coll.with_options(write_concern=WriteConcern(w=0))
        unack.insert_one(docs[3])
        unack.insert_many(docs[4:], ordered=False)
        wait_until(lambda: self.db.test.count_documents({}) == len(docs),
                   'insert documents with w=0')

        # Database.command auto decrypts.
        res = client.pymongo_test.command(
            'find', 'test', filter={'ssn': '000'})
        decrypted_docs = res['cursor']['firstBatch']
        self.assertEqual(decrypted_docs, [{'_id': 0, 'ssn': '000'}])

        # Collection.find auto decrypts.
        decrypted_docs = list(encrypted_coll.find())
        self.assertEqual(decrypted_docs, docs)

        # Collection.find auto decrypts getMores.
        decrypted_docs = list(encrypted_coll.find(batch_size=1))
        self.assertEqual(decrypted_docs, docs)

        # Collection.aggregate auto decrypts.
        decrypted_docs = list(encrypted_coll.aggregate([]))
        self.assertEqual(decrypted_docs, docs)

        # Collection.aggregate auto decrypts getMores.
        decrypted_docs = list(encrypted_coll.aggregate([], batchSize=1))
        self.assertEqual(decrypted_docs, docs)

        # Collection.distinct auto decrypts.
        decrypted_ssns = encrypted_coll.distinct('ssn')
        self.assertEqual(decrypted_ssns, [d['ssn'] for d in docs])

        # Make sure the field is actually encrypted.
        for encrypted_doc in self.db.test.find():
            self.assertIsInstance(encrypted_doc['_id'], int)
            self.assertEncrypted(encrypted_doc['ssn'])

        # Attempt to encrypt an unencodable object.
        with self.assertRaises(BSONError):
            encrypted_coll.insert_one({'unencodeable': object()})

    def test_auto_encrypt(self):
        # Configure the encrypted field via jsonSchema.
        json_schema = json_data('custom', 'schema.json')
        coll = self.db.create_collection(
            'test', validator={'$jsonSchema': json_schema}, codec_options=OPTS)
        self.addCleanup(coll.drop)

        opts = AutoEncryptionOpts(KMS_PROVIDERS, 'admin.datakeys')
        self._test_auto_encrypt(opts)

    def test_auto_encrypt_local_schema_map(self):
        # Configure the encrypted field via the local schema_map option.
        schemas = {'pymongo_test.test': json_data('custom', 'schema.json')}
        opts = AutoEncryptionOpts(
            KMS_PROVIDERS, 'admin.datakeys', schema_map=schemas)

        self._test_auto_encrypt(opts)


class TestExplicitSimple(EncryptionIntegrationTest):

    def test_encrypt_decrypt(self):
        client_encryption = ClientEncryption(
            KMS_PROVIDERS, 'admin.datakeys', client_context.client)
        self.addCleanup(client_encryption.close)
        # Use standard UUID representation.
        key_vault = client_context.client.admin.get_collection(
            'datakeys', codec_options=OPTS)
        self.addCleanup(key_vault.drop)

        # Create the encrypted field's data key.
        key_id = client_encryption.create_data_key(
            'local', key_alt_names=['name'])
        self.assertIsInstance(key_id, uuid.UUID)
        self.assertTrue(key_vault.find_one({'_id': key_id}))

        # Create an unused data key to make sure filtering works.
        unused_key_id = client_encryption.create_data_key(
            'local', key_alt_names=['unused'])
        self.assertIsInstance(unused_key_id, uuid.UUID)
        self.assertTrue(key_vault.find_one({'_id': unused_key_id}))

        doc = {'_id': 0, 'ssn': '000'}
        encrypted_ssn = client_encryption.encrypt(
            doc['ssn'], Algorithm.Deterministic, key_id=key_id)

        # Ensure encryption via key_alt_name for the same key produces the
        # same output.
        encrypted_ssn2 = client_encryption.encrypt(
            doc['ssn'], Algorithm.Deterministic, key_alt_name='name')
        self.assertEqual(encrypted_ssn, encrypted_ssn2)

        # Test decryption.
        decrypted_ssn = client_encryption.decrypt(encrypted_ssn)
        self.assertEqual(decrypted_ssn, doc['ssn'])

    def test_validation(self):
        client_encryption = ClientEncryption(
            KMS_PROVIDERS, 'admin.datakeys', client_context.client)
        self.addCleanup(client_encryption.close)

        msg = 'value to decrypt must be a bson.binary.Binary with subtype 6'
        with self.assertRaisesRegex(TypeError, msg):
            client_encryption.decrypt('str')
        with self.assertRaisesRegex(TypeError, msg):
            client_encryption.decrypt(Binary(b'123'))

    def test_bson_errors(self):
        client_encryption = ClientEncryption(
            KMS_PROVIDERS, 'admin.datakeys', client_context.client)
        self.addCleanup(client_encryption.close)

        # Attempt to encrypt an unencodable object.
        unencodable_value = object()
        with self.assertRaises(BSONError):
            client_encryption.encrypt(
                unencodable_value, Algorithm.Deterministic,
                key_id=Binary(uuid.uuid4().bytes, UUID_SUBTYPE))


# Spec tests

AWS_CREDS = {
    'accessKeyId': os.environ.get('FLE_AWS_KEY', ''),
    'secretAccessKey': os.environ.get('FLE_AWS_SECRET', '')
}


class TestSpec(SpecRunner):

    @classmethod
    @unittest.skipUnless(_HAVE_PYMONGOCRYPT, 'pymongocrypt is not installed')
    def setUpClass(cls):
        super(TestSpec, cls).setUpClass()

    def parse_auto_encrypt_opts(self, opts):
        """Parse clientOptions.autoEncryptOpts."""
        opts = camel_to_snake_args(opts)
        kms_providers = opts['kms_providers']
        if 'aws' in kms_providers:
            kms_providers['aws'] = AWS_CREDS
            if not any(AWS_CREDS.values()):
                self.skipTest('AWS environment credentials are not set')
        if 'key_vault_namespace' not in opts:
            opts['key_vault_namespace'] = 'admin.datakeys'
        opts = dict(opts)
        return AutoEncryptionOpts(**opts)

    def parse_client_options(self, opts):
        """Override clientOptions parsing to support autoEncryptOpts."""
        encrypt_opts = opts.pop('autoEncryptOpts')
        if encrypt_opts:
            opts['auto_encryption_opts'] = self.parse_auto_encrypt_opts(
                encrypt_opts)

        return super(TestSpec, self).parse_client_options(opts)

    def get_object_name(self, op):
        """Default object is collection."""
        return op.get('object', 'collection')

    def maybe_skip_scenario(self, test):
        super(TestSpec, self).maybe_skip_scenario(test)
        if 'type=symbol' in test['description'].lower():
            raise unittest.SkipTest(
                'PyMongo does not support the symbol type')

    def setup_scenario(self, scenario_def):
        """Override a test's setup."""
        key_vault_data = scenario_def['key_vault_data']
        if key_vault_data:
            coll = client_context.client.get_database(
                'admin',
                write_concern=WriteConcern(w='majority'),
                codec_options=OPTS)['datakeys']
            coll.drop()
            coll.insert_many(key_vault_data)

        db_name = self.get_scenario_db_name(scenario_def)
        coll_name = self.get_scenario_coll_name(scenario_def)
        db = client_context.client.get_database(
            db_name, write_concern=WriteConcern(w='majority'),
            codec_options=OPTS)
        coll = db[coll_name]
        coll.drop()
        json_schema = scenario_def['json_schema']
        if json_schema:
            db.create_collection(
                coll_name,
                validator={'$jsonSchema': json_schema}, codec_options=OPTS)
        else:
            db.create_collection(coll_name)

        if scenario_def['data']:
            # Load data.
            coll.insert_many(scenario_def['data'])

    def allowable_errors(self, op):
        """Override expected error classes."""
        errors = super(TestSpec, self).allowable_errors(op)
        # An updateOne test expects encryption to error when no $ operator
        # appears but pymongo raises a client side ValueError in this case.
        if op['name'] == 'updateOne':
            errors += (ValueError,)
        return errors


def create_test(scenario_def, test, name):
    @client_context.require_test_commands
    def run_scenario(self):
        self.run_scenario(scenario_def, test)

    return run_scenario


test_creator = TestCreator(create_test, TestSpec, SPEC_PATH)
test_creator.create_tests()


# Prose Tests
LOCAL_MASTER_KEY = base64.b64decode(
    b'Mng0NCt4ZHVUYUJCa1kxNkVyNUR1QURhZ2h2UzR2d2RrZzh0cFBwM3R6NmdWMDFBMUN3YkQ'
    b'5aXRRMkhGRGdQV09wOGVNYUMxT2k3NjZKelhaQmRCZGJkTXVyZG9uSjFk')

LOCAL_KEY_ID = Binary(
    base64.b64decode(b'LOCALAAAAAAAAAAAAAAAAA=='), UUID_SUBTYPE)
AWS_KEY_ID = Binary(
    base64.b64decode(b'AWSAAAAAAAAAAAAAAAAAAA=='), UUID_SUBTYPE)


def create_with_schema(coll, json_schema):
    """Create and return a Collection with a jsonSchema."""
    coll.drop()
    return coll.database.create_collection(
        coll.name, validator={'$jsonSchema': json_schema}, codec_options=OPTS)


def create_key_vault(vault, *data_keys):
    """Create the key vault collection with optional data keys."""
    vault = vault.with_options(
        write_concern=WriteConcern(w='majority'),
        codec_options=OPTS)
    vault.drop()
    if data_keys:
        vault.insert_many(data_keys)
    return vault


class TestDataKeyDoubleEncryption(EncryptionIntegrationTest):

    @classmethod
    @unittest.skipUnless(all(AWS_CREDS.values()),
                         'AWS environment credentials are not set')
    def setUpClass(cls):
        super(TestDataKeyDoubleEncryption, cls).setUpClass()

    @staticmethod
    def kms_providers():
        return {'aws': AWS_CREDS, 'local': {'key': LOCAL_MASTER_KEY}}

    def test_data_key(self):
        self.client.db.coll.drop()
        vault = create_key_vault(self.client.admin.datakeys)
        self.addCleanup(vault.drop)

        # Configure the encrypted field via the local schema_map option.
        schemas = {
          "db.coll": {
            "bsonType": "object",
            "properties": {
              "encrypted_placeholder": {
                "encrypt": {
                  "keyId": "/placeholder",
                  "bsonType": "string",
                  "algorithm": "AEAD_AES_256_CBC_HMAC_SHA_512-Random"
                }
              }
            }
          }
        }
        opts = AutoEncryptionOpts(
            self.kms_providers(), 'admin.datakeys', schema_map=schemas)
        client_encrypted = rs_or_single_client(
            auto_encryption_opts=opts, uuidRepresentation='standard')
        self.addCleanup(client_encrypted.close)

        client_encryption = ClientEncryption(
            self.kms_providers(), 'admin.datakeys', client_context.client)
        self.addCleanup(client_encryption.close)

        # Local create data key.
        local_datakey_id = client_encryption.create_data_key(
            'local', key_alt_names=['local_altname'])
        self.assertIsInstance(local_datakey_id, uuid.UUID)
        docs = list(vault.find({'_id': local_datakey_id}))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['masterKey']['provider'], 'local')

        # Local encrypt by key_id.
        local_encrypted = client_encryption.encrypt(
            'hello local', Algorithm.Deterministic, key_id=local_datakey_id)
        self.assertEncrypted(local_encrypted)
        client_encrypted.db.coll.insert_one(
            {'_id': 'local', 'value': local_encrypted})
        doc_decrypted = client_encrypted.db.coll.find_one({'_id': 'local'})
        self.assertEqual(doc_decrypted['value'], 'hello local')

        # Local encrypt by key_alt_name.
        local_encrypted_altname = client_encryption.encrypt(
            'hello local', Algorithm.Deterministic,
            key_alt_name='local_altname')
        self.assertEqual(local_encrypted_altname, local_encrypted)

        # AWS create data key.
        master_key = {
            'region': 'us-east-1',
            'key': 'arn:aws:kms:us-east-1:579766882180:key/89fcc2c4-08b0-4bd9-'
                   '9f25-e30687b580d0'
        }
        aws_datakey_id = client_encryption.create_data_key(
            'aws', master_key=master_key, key_alt_names=['aws_altname'])
        self.assertIsInstance(aws_datakey_id, uuid.UUID)
        docs = list(vault.find({'_id': aws_datakey_id}))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['masterKey']['provider'], 'aws')

        # AWS encrypt by key_id.
        aws_encrypted = client_encryption.encrypt(
            'hello aws', Algorithm.Deterministic, key_id=aws_datakey_id)
        self.assertEncrypted(aws_encrypted)
        client_encrypted.db.coll.insert_one(
            {'_id': 'aws', 'value': aws_encrypted})
        doc_decrypted = client_encrypted.db.coll.find_one({'_id': 'aws'})
        self.assertEqual(doc_decrypted['value'], 'hello aws')

        # AWS encrypt by key_alt_name.
        aws_encrypted_altname = client_encryption.encrypt(
            'hello aws', Algorithm.Deterministic, key_alt_name='aws_altname')
        self.assertEqual(aws_encrypted_altname, aws_encrypted)

        # Explicitly encrypting an auto encrypted field.
        msg = ('Cannot encrypt element of type binData because schema '
               'requires that type is one of: \[ string \]')
        with self.assertRaisesRegex(EncryptionError, msg):
            client_encrypted.db.coll.insert_one(
                {'encrypted_placeholder': local_encrypted})


class TestExternalKeyVault(EncryptionIntegrationTest):

    @staticmethod
    def kms_providers():
        return {'local': {'key': LOCAL_MASTER_KEY}}

    def _test_external_key_vault(self, with_external_key_vault):
        self.client.db.coll.drop()
        vault = create_key_vault(
            self.client.admin.datakeys,
            json_data('corpus', 'corpus-key-local.json'),
            json_data('corpus', 'corpus-key-aws.json'))
        self.addCleanup(vault.drop)

        # Configure the encrypted field via the local schema_map option.
        schemas = {'db.coll': json_data('external', 'external-schema.json')}
        if with_external_key_vault:
            key_vault_client = rs_or_single_client(
                username='fake-user', password='fake-pwd')
            self.addCleanup(key_vault_client.close)
        else:
            key_vault_client = client_context.client
        opts = AutoEncryptionOpts(
            self.kms_providers(), 'admin.datakeys', schema_map=schemas,
            key_vault_client=key_vault_client)

        client_encrypted = rs_or_single_client(
            auto_encryption_opts=opts, uuidRepresentation='standard')
        self.addCleanup(client_encrypted.close)

        client_encryption = ClientEncryption(
            self.kms_providers(), 'admin.datakeys', key_vault_client)
        self.addCleanup(client_encryption.close)

        if with_external_key_vault:
            # Authentication error.
            with self.assertRaises(EncryptionError) as ctx:
                client_encrypted.db.coll.insert_one({"encrypted": "test"})
            # AuthenticationFailed error.
            self.assertIsInstance(ctx.exception.cause, OperationFailure)
            self.assertEqual(ctx.exception.cause.code, 18)
        else:
            client_encrypted.db.coll.insert_one({"encrypted": "test"})

        if with_external_key_vault:
            # Authentication error.
            with self.assertRaises(EncryptionError) as ctx:
                client_encryption.encrypt(
                    "test", Algorithm.Deterministic, key_id=LOCAL_KEY_ID)
            # AuthenticationFailed error.
            self.assertIsInstance(ctx.exception.cause, OperationFailure)
            self.assertEqual(ctx.exception.cause.code, 18)
        else:
            client_encryption.encrypt(
                "test", Algorithm.Deterministic, key_id=LOCAL_KEY_ID)

    def test_external_key_vault_1(self):
        self._test_external_key_vault(True)

    def test_external_key_vault_2(self):
        self._test_external_key_vault(False)


class TestCorpus(EncryptionIntegrationTest):

    @classmethod
    @unittest.skipUnless(all(AWS_CREDS.values()),
                         'AWS environment credentials are not set')
    def setUpClass(cls):
        super(TestCorpus, cls).setUpClass()

    @staticmethod
    def kms_providers():
        return {'aws': AWS_CREDS, 'local': {'key': LOCAL_MASTER_KEY}}

    @staticmethod
    def fix_up_schema(json_schema):
        """Remove deprecated symbol/dbPointer types from json schema."""
        for key in json_schema['properties'].keys():
            if '_symbol_' in key or '_dbPointer_' in key:
                del json_schema['properties'][key]
        return json_schema

    @staticmethod
    def fix_up_curpus(corpus):
        """Disallow deprecated symbol/dbPointer types from corpus test."""
        for key in corpus:
            if '_symbol_' in key or '_dbPointer_' in key:
                corpus[key]['allowed'] = False
        return corpus

    @staticmethod
    def fix_up_curpus_encrypted(corpus_encrypted, corpus):
        """Fix the expected values for deprecated symbol/dbPointer types."""
        for key in corpus_encrypted:
            if '_symbol_' in key or '_dbPointer_' in key:
                corpus_encrypted[key] = copy.deepcopy(corpus[key])
        return corpus_encrypted

    def _test_corpus(self, opts):
        # Drop and create the collection 'db.coll' with jsonSchema.
        coll = create_with_schema(
            self.client.db.coll,
            self.fix_up_schema(json_data('corpus', 'corpus-schema.json')))
        self.addCleanup(coll.drop)

        vault = create_key_vault(
            self.client.admin.datakeys,
            json_data('corpus', 'corpus-key-local.json'),
            json_data('corpus', 'corpus-key-aws.json'))
        self.addCleanup(vault.drop)

        client_encrypted = rs_or_single_client(
            auto_encryption_opts=opts, uuidRepresentation='standard')
        self.addCleanup(client_encrypted.close)

        client_encryption = ClientEncryption(
            self.kms_providers(), 'admin.datakeys', client_context.client)
        self.addCleanup(client_encryption.close)

        corpus = self.fix_up_curpus(json_data('corpus', 'corpus.json'))
        corpus_copied = SON()
        for key, value in corpus.items():
            corpus_copied[key] = copy.deepcopy(value)
            if key in ('_id', 'altname_aws', 'altname_local'):
                continue
            if value['method'] == 'auto':
                continue
            if value['method'] == 'explicit':
                identifier = value['identifier']
                self.assertIn(identifier, ('id', 'altname'))
                kms = value['kms']
                self.assertIn(kms, ('local', 'aws'))
                if identifier == 'id':
                    if kms == 'local':
                        kwargs = dict(key_id=LOCAL_KEY_ID)
                    else:
                        kwargs = dict(key_id=AWS_KEY_ID)
                else:
                    kwargs = dict(key_alt_name=kms)

                self.assertIn(value['algo'], ('det', 'rand'))
                if value['algo'] == 'det':
                    algo = Algorithm.Deterministic
                else:
                    algo = Algorithm.Random

                try:
                    encrypted_val = client_encryption.encrypt(
                        value['value'], algo, **kwargs)
                    if not value['allowed']:
                        self.fail('encrypt should have failed: %r: %r' % (
                            key, value))
                    corpus_copied[key]['value'] = encrypted_val
                except Exception:
                    if value['allowed']:
                        tb = traceback.format_exc()
                        self.fail('encrypt failed: %r: %r, traceback: %s' % (
                            key, value, tb))

        client_encrypted.db.coll.insert_one(corpus_copied)
        corpus_decrypted = client_encrypted.db.coll.find_one()
        self.assertEqual(corpus_decrypted, corpus)

        corpus_encrypted_expected = self.fix_up_curpus_encrypted(json_data(
            'corpus', 'corpus-encrypted.json'), corpus)
        corpus_encrypted_actual = coll.find_one()
        for key, value in corpus_encrypted_actual.items():
            if key in ('_id', 'altname_aws', 'altname_local'):
                continue

            if value['algo'] == 'det':
                self.assertEqual(
                    value['value'], corpus_encrypted_expected[key]['value'],
                    key)
            elif value['algo'] == 'rand' and value['allowed']:
                self.assertNotEqual(
                    value['value'], corpus_encrypted_expected[key]['value'],
                    key)

            if value['allowed']:
                decrypt_actual = client_encryption.decrypt(value['value'])
                decrypt_expected = client_encryption.decrypt(
                    corpus_encrypted_expected[key]['value'])
                self.assertEqual(decrypt_actual, decrypt_expected, key)
            else:
                self.assertEqual(value['value'], corpus[key]['value'], key)

    def test_corpus(self):
        opts = AutoEncryptionOpts(self.kms_providers(), 'admin.datakeys')
        self._test_corpus(opts)

    def test_corpus_local_schema(self):
        # Configure the encrypted field via the local schema_map option.
        schemas = {'db.coll': self.fix_up_schema(
            json_data('corpus', 'corpus-schema.json'))}
        opts = AutoEncryptionOpts(
            self.kms_providers(), 'admin.datakeys', schema_map=schemas)
        self._test_corpus(opts)


if __name__ == "__main__":
    unittest.main()