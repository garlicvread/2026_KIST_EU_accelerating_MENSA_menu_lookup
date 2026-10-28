"""Drive 게시기는 제어된 rclone 포트를 통해 완전한 공개 묶음과 파일 식별자를 확인합니다."""

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from mensa import drive_publication
from mensa.releases import _json_bytes, _release_id
from mensa.translation_contract import COMPONENT_POLICY_VERSION, NAME_POLICY_VERSION, source_for
from scripts.menu_source import parse_menu
from test_menu_source import page
from test_publication import translation_entry


REMOTE = 'handover-drive:'
FOLDER_ID = 'configured_folder_12345'
FILE_ID = 'current_file_12345'
OTHER_FOLDER_ID = 'unrelated_folder_12345'


def envelope(menu, cache):
    return {
        'schema_version': 1,
        'release_id': _release_id(_json_bytes(menu), _json_bytes(cache)),
        'menu': menu,
        'translations': cache,
    }


class FakeRclone:
    """이 포트는 lsjson의 ID·MD5와 cat/copyto의 파일 내용을 모사하며 실제 원격 명령을 실행하지 않습니다."""

    def __init__(self):
        self.folders = {FOLDER_ID: [], OTHER_FOLDER_ID: []}
        self.commands = []
        self.uploads = []
        self.fail_operation = None
        self.on_copy = None
        self.on_cat = None
        self.on_list = None

    def add(self, body, *, name='current.json', file_id=FILE_ID, folder_id=FOLDER_ID,
            is_dir=False):
        item = {'ID': file_id, 'Name': name, 'Path': name, 'IsDir': is_dir,
                'body': body if isinstance(body, str) else _json_bytes(body).decode('utf-8')}
        self.folders[folder_id].append(item)
        return item

    def __call__(self, argv):
        self.commands.append(list(argv))
        if argv[0] != 'rclone' or argv[1] not in {'lsjson', 'cat', 'copyto'}:
            raise AssertionError(f'Unexpected command: {argv}')
        if argv.count('--drive-root-folder-id') != 1:
            raise AssertionError('Every rclone operation must select exactly one Drive folder')
        folder_id = argv[argv.index('--drive-root-folder-id') + 1]
        if folder_id != FOLDER_ID:
            raise AssertionError('The command selected an unrelated Drive folder')
        operation = argv[1]
        if operation == self.fail_operation:
            raise RuntimeError('upstream access_token=PRIVATE-CREDENTIAL')
        items = self.folders[folder_id]
        if operation == 'lsjson':
            if argv[2] != REMOTE or '--hash' not in argv or '--recursive' in argv:
                raise AssertionError('Listing must inspect only the configured root with hashes')
            if self.on_list is not None:
                replacement = self.on_list(items)
                if replacement is not None:
                    return replacement
            return json.dumps([
                {**{key: item[key] for key in ('ID', 'Name', 'Path', 'IsDir')},
                 'Size': len(item['body'].encode('utf-8')),
                 'Hashes': {'MD5': hashlib.md5(item['body'].encode('utf-8')).hexdigest()}}
                for item in items
            ])
        if operation == 'cat':
            if argv[2] != REMOTE + 'current.json':
                raise AssertionError('The publisher read a different remote file')
            body = next(item['body'] for item in items if item['Name'] == 'current.json')
            return body if self.on_cat is None else self.on_cat(body)
        if argv[3] != REMOTE + 'current.json':
            raise AssertionError('The publisher wrote a different remote file')
        payload = Path(argv[2]).read_text(encoding='utf-8')
        self.uploads.append(payload)
        current = next((item for item in items if item['Name'] == 'current.json'), None)
        if current is None:
            current = self.add(payload)
        else:
            current['body'] = payload
        if self.on_copy is not None:
            self.on_copy(current)
        return ''


class DrivePublisherTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.key = record['translation_key']
        self.cache = {
            'schema_version': 1,
            'entries': {self.key: translation_entry(source_for(record))},
            'notices': {
                'vegan': {'en': 'Vegan', 'ko': '비건'},
                'Weizen': {'en': 'Wheat', 'ko': '밀'},
                'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
                'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
            },
        }
        self.command = FakeRclone()

    def module(self):
        return drive_publication

    def publisher(self, *, file_id=None, command=None):
        return self.module().DrivePublisher(REMOTE, FOLDER_ID, file_id=file_id,
                                            command=self.command if command is None else command)

    def changed_pair(self):
        menu, cache = copy.deepcopy(self.menu), copy.deepcopy(self.cache)
        menu['source']['fetched_at'] = '2026-09-21T11:00:00Z'
        cache['entries'][self.key]['en']['name'] = 'New reviewed dish'
        return menu, cache

    def assert_no_upload(self):
        self.assertEqual(self.command.uploads, [])

    def test_bootstrap_publishes_public_envelope_and_preserves_inputs_and_other_files(self):
        other = self.command.add('leave me alone', name='operator-notes.txt', file_id='other_file_12345')
        outside = self.command.add('separate root', folder_id=OTHER_FOLDER_ID)
        original = copy.deepcopy((self.menu, self.cache, other, outside))
        publisher = self.publisher()
        self.assertIsNone(publisher.load_current())
        result = publisher.publish(self.menu, self.cache, today='2026-09-21')
        self.assertEqual(result, self.module().DrivePublication(envelope(self.menu, self.cache)['release_id'], FILE_ID))
        with self.assertRaises(FrozenInstanceError):
            result.file_id = 'different_file_12345'
        self.assertEqual(json.loads(self.command.uploads[0]), envelope(self.menu, self.cache))
        self.assertEqual(publisher.load_current(), (self.menu, self.cache))
        self.assertEqual((self.menu, self.cache, other, outside), original)
        self.assertEqual(len(self.command.folders[FOLDER_ID]), 2)
        temporary = next(argv[2] for argv in self.command.commands if argv[1] == 'copyto')
        self.assertFalse(Path(temporary).exists())

    def test_reuses_valid_existing_file_and_skips_identical_payload(self):
        self.command.add(envelope(self.menu, self.cache))
        publisher = self.publisher()
        first = publisher.publish(self.menu, self.cache)
        second = self.publisher(file_id=FILE_ID).publish(copy.deepcopy(self.menu), copy.deepcopy(self.cache))
        self.assertEqual(first, second)
        self.assertEqual(first.file_id, FILE_ID)
        self.assert_no_upload()

    def test_update_preserves_file_identity_and_checks_readback(self):
        self.command.add(envelope(self.menu, self.cache))
        changed = self.changed_pair()
        publisher = self.publisher(file_id=FILE_ID)
        result = publisher.publish(*changed, previous=self.menu, today='2026-09-21')
        self.assertEqual(result.release_id, envelope(*changed)['release_id'])
        self.assertEqual(result.file_id, FILE_ID)
        self.assertEqual(publisher.load_current(), changed)
        self.assertEqual(len(self.command.uploads), 1)
        copy_index = next(index for index, argv in enumerate(self.command.commands) if argv[1] == 'copyto')
        self.assertIn('cat', [argv[1] for argv in self.command.commands[copy_index + 1:]])

    def test_invalid_or_partial_pair_is_refused_before_remote_access(self):
        cases = []
        incomplete = copy.deepcopy(self.cache)
        incomplete['entries'].clear()
        cases.append((self.menu, incomplete, {}))
        invalid_cache = copy.deepcopy(self.cache)
        del invalid_cache['entries'][self.key]['ko']
        cases.append((self.menu, invalid_cache, {}))
        wrong_source = copy.deepcopy(self.cache)
        wrong_source['entries'][self.key]['source']['name_de'] = 'Different meal'
        cases.append((self.menu, wrong_source, {}))
        cases.append((self.menu, self.cache, {'today': '2026-09-22'}))
        for index, (menu, cache, options) in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    self.publisher().publish(menu, cache, **options)
                self.assertEqual(self.command.commands, [])

    def test_previous_menu_contract_is_checked_without_mutating_previous(self):
        previous = copy.deepcopy(self.menu)
        original = copy.deepcopy(previous)
        pending = copy.deepcopy(self.menu)
        record = pending['days'][0]['meals'][0]
        record['prices'] = None
        record['price_status'] = 'source_pending'
        record['price_source']['raw'] = None
        with self.assertRaises(ValueError):
            self.publisher().publish(pending, self.cache, previous=previous)
        self.assertEqual(previous, original)
        self.assert_no_upload()

    def test_private_cache_fields_are_refused_in_inputs_and_existing_remote_bundles(self):
        for location in ('cache', 'entry'):
            with self.subTest(location=location):
                command = FakeRclone()
                cache = copy.deepcopy(self.cache)
                container = cache if location == 'cache' else cache['entries'][self.key]
                container['private_checkpoint'] = {'attempt': 7, 'access_token': 'PRIVATE-CREDENTIAL'}
                original = copy.deepcopy(cache)
                with self.assertRaises(ValueError):
                    self.publisher(command=command).publish(self.menu, cache)
                self.assertEqual(command.commands, [])
                item = command.add(envelope(self.menu, cache))
                saved = copy.deepcopy(item)
                with self.assertRaises(ValueError):
                    self.publisher(command=command).load_current()
                with self.assertRaises(ValueError):
                    self.publisher(command=command).publish(*self.changed_pair())
                self.assertEqual(command.uploads, [])
                self.assertEqual(item, saved)
                self.assertEqual(cache, original)

    def test_public_cache_policy_and_generation_identity_fields_are_preserved(self):
        cache = copy.deepcopy(self.cache)
        entry = cache['entries'][self.key]
        entry['name_policy_version'] = NAME_POLICY_VERSION
        entry['component_policy_version'] = COMPONENT_POLICY_VERSION
        entry['generation_identity'] = {'schema_version': 1, 'policy': 'a' * 64,
                                        'glossary': 'b' * 64, 'backend': 'c' * 64}
        publisher = self.publisher()
        publisher.publish(self.menu, cache)
        self.assertEqual(publisher.load_current(), (self.menu, cache))

    def test_wrong_expected_file_id_and_missing_expected_file_stop_before_upload(self):
        self.command.add(envelope(self.menu, self.cache))
        publisher = self.publisher(file_id='expected_other_file_12345')
        for operation in (publisher.load_current, lambda: publisher.publish(*self.changed_pair())):
            with self.assertRaises(ValueError):
                operation()
        self.command.folders[FOLDER_ID].clear()
        with self.assertRaises(ValueError):
            publisher.publish(self.menu, self.cache)
        self.assert_no_upload()

    def test_duplicate_reserved_names_or_reserved_directory_stop_before_upload(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                self.command.folders[FOLDER_ID].clear()
                self.command.add(envelope(self.menu, self.cache), is_dir=directory)
                if not directory:
                    self.command.add(envelope(self.menu, self.cache), file_id='duplicate_file_12345')
                publisher = self.publisher()
                with self.assertRaises(ValueError):
                    publisher.load_current()
                with self.assertRaises(ValueError):
                    publisher.publish(*self.changed_pair())
                self.assert_no_upload()

    def test_corrupt_or_unrecognized_existing_content_is_not_overwritten(self):
        wrong_id = envelope(self.menu, self.cache)
        wrong_id['release_id'] = '0' * 64
        incomplete = envelope(self.menu, self.cache)
        incomplete['translations'] = {}
        extra = envelope(self.menu, self.cache)
        extra['private_checkpoint'] = {'attempt': 7}
        boolean_version = envelope(self.menu, self.cache)
        boolean_version['schema_version'] = True
        for index, body in enumerate(('not json', {}, wrong_id, incomplete, extra, boolean_version)):
            with self.subTest(case=index):
                self.command.folders[FOLDER_ID].clear()
                item = self.command.add(body)
                original = copy.deepcopy(item)
                publisher = self.publisher()
                with self.assertRaises(ValueError):
                    publisher.load_current()
                with self.assertRaises(ValueError):
                    publisher.publish(*self.changed_pair())
                self.assertEqual(item, original)
                self.assert_no_upload()

    def test_malformed_listing_or_missing_hash_or_id_is_refused_before_mutation(self):
        self.command.add(envelope(self.menu, self.cache))
        valid = json.loads(self.command(['rclone', 'lsjson', REMOTE, '--hash', '--drive-root-folder-id', FOLDER_ID]))
        missing_hash = copy.deepcopy(valid)
        missing_hash[0]['Hashes'] = {}
        missing_id = copy.deepcopy(valid)
        missing_id[0].pop('ID')
        nested = copy.deepcopy(valid)
        nested[0]['Path'] = 'nested/current.json'
        for listing in ('broken json', '{}', json.dumps(missing_hash), json.dumps(missing_id), json.dumps(nested)):
            with self.subTest(listing=listing):
                self.command.on_list = lambda _, value=listing: value
                with self.assertRaises(ValueError):
                    self.publisher().publish(*self.changed_pair())
                self.assert_no_upload()

    def test_cat_must_match_listed_hash_before_existing_content_is_trusted(self):
        self.command.add(envelope(self.menu, self.cache))
        self.command.on_cat = lambda _: _json_bytes(envelope(*self.changed_pair())).decode('utf-8')
        with self.assertRaises(ValueError):
            self.publisher().publish(*self.changed_pair())
        self.assert_no_upload()

    def test_listing_and_initial_read_failures_are_redacted_before_any_upload(self):
        self.command.add(envelope(self.menu, self.cache))
        for operation in ('lsjson', 'cat'):
            with self.subTest(operation=operation):
                self.command.fail_operation = operation
                with self.assertRaises(RuntimeError) as caught:
                    self.publisher().publish(*self.changed_pair())
                self.assertNotIn('PRIVATE-CREDENTIAL', str(caught.exception))
                self.assert_no_upload()

    def test_upload_failure_is_redacted_and_preserves_existing_file_and_inputs(self):
        item = self.command.add(envelope(self.menu, self.cache))
        original = copy.deepcopy(item)
        changed = self.changed_pair()
        unchanged = copy.deepcopy(changed)
        self.command.fail_operation = 'copyto'
        with self.assertRaises(RuntimeError) as caught:
            self.publisher(file_id=FILE_ID).publish(*changed)
        self.assertNotIn('PRIVATE-CREDENTIAL', str(caught.exception))
        self.assertNotIn('access_token', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(item, original)
        self.assertEqual(changed, unchanged)
        self.assert_no_upload()
        temporary = next(argv[2] for argv in self.command.commands if argv[1] == 'copyto')
        self.assertFalse(Path(temporary).exists())

    def test_readback_failure_does_not_delete_retry_or_report_success(self):
        self.command.add(envelope(self.menu, self.cache))
        self.command.on_copy = lambda _: setattr(self.command, 'fail_operation', 'cat')
        with self.assertRaises(RuntimeError):
            self.publisher(file_id=FILE_ID).publish(*self.changed_pair())
        self.assertEqual(len(self.command.uploads), 1)
        self.assertEqual(len(self.command.folders[FOLDER_ID]), 1)
        self.command.fail_operation = None
        self.command.on_copy = None
        result = self.publisher(file_id=FILE_ID).publish(*self.changed_pair())
        self.assertEqual(result.file_id, FILE_ID)
        self.assertEqual(len(self.command.uploads), 1)

    def test_changed_file_identity_or_content_after_upload_is_rejected(self):
        for change in ('identity', 'content', 'duplicate', 'missing'):
            with self.subTest(change=change):
                command = FakeRclone()
                command.add(envelope(self.menu, self.cache))
                def change_after_copy(item):
                    if change == 'identity':
                        item['ID'] = 'unexpected_new_file_12345'
                    elif change == 'content':
                        item['body'] = _json_bytes(envelope(self.menu, self.cache)).decode('utf-8')
                    elif change == 'duplicate':
                        command.add(item['body'], file_id='duplicate_file_12345')
                    else:
                        command.folders[FOLDER_ID].clear()
                command.on_copy = change_after_copy
                with self.assertRaises(ValueError):
                    self.publisher(file_id=FILE_ID, command=command).publish(*self.changed_pair())
                self.assertEqual(len(command.uploads), 1)

    def test_safe_explicit_configuration_is_required_before_commands(self):
        publisher_type = self.module().DrivePublisher
        for remote in ('', 'gdrive:subfolder', ':drive:', '--config=secret:', 'gdrive:\n'):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                publisher_type(remote, FOLDER_ID, command=self.command)
        for folder in ('', '../other', '--password=secret', 'short'):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                publisher_type(REMOTE, folder, command=self.command)
        with self.assertRaises(ValueError):
            publisher_type(REMOTE, FOLDER_ID, file_id='../other', command=self.command)
        with self.assertRaises(ValueError):
            publisher_type(REMOTE, FOLDER_ID, command='rclone')
        self.assertEqual(self.command.commands, [])

    def test_default_subprocess_is_bounded_uses_argv_and_suppresses_upstream_errors(self):
        publisher = self.module().DrivePublisher(REMOTE, FOLDER_ID)
        with patch('mensa.drive_publication.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '[]')) as run:
            self.assertIsNone(publisher.load_current())
        args, options = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][0:3], ['rclone', 'lsjson', REMOTE])
        self.assertFalse(options.get('shell', False))
        self.assertGreater(options['timeout'], 0)
        self.assertLessEqual(options['timeout'], 180)
        self.assertIs(options['stderr'], subprocess.DEVNULL)
        failure = subprocess.TimeoutExpired(['rclone'], 120, output='PRIVATE-CREDENTIAL')
        with patch('mensa.drive_publication.subprocess.run', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                publisher.load_current()
        self.assertNotIn('PRIVATE-CREDENTIAL', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
