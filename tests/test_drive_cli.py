"""기본 Mac 실행이 Drive 설정을 사용하며 설정 누락 시 Git 게시로 되돌아가지 않는지 검사합니다."""

from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import local_refresh as worker
from mensa.config import PublicationSettings
import test_directory_refresh as directory_fixture


class DriveCliTests(unittest.TestCase):
    def test_missing_base_config_refuses_git_fallback(self):
        with TemporaryDirectory() as folder, patch.object(worker, 'RefreshJob') as legacy:
            with self.assertRaisesRegex(ValueError, 'worker.toml'):
                worker.main(['--base', folder])
            legacy.assert_not_called()

    def test_base_status_remains_read_only_without_configuration(self):
        with TemporaryDirectory() as folder:
            before = set(Path(folder).iterdir())
            output = io.StringIO()
            with redirect_stdout(output):
                worker.main(['--base', folder, '--status'])
            self.assertEqual(json.loads(output.getvalue())['queue']['pending'], None)
            self.assertEqual(before, set(Path(folder).iterdir()))

    def test_explicit_drive_config_routes_to_drive_job_without_git_job(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            path = f.checkout.parent/'worker.toml'
            path.write_text(path.read_text().replace('mode="directory"', '''mode="drive"
remote="fixture:"
folder_id="folder_1234567890"
file_id="snapshot_1234567890"'''))
            marker = object()
            with patch.object(worker, 'DriveRefreshJob', return_value=marker) as drive, \
                    patch.object(worker, 'ConfiguredRefreshJob') as git, \
                    patch.object(worker, 'process_queue', return_value={'status': 'idle'}) as run, \
                    redirect_stdout(io.StringIO()):
                worker.main(['--config', str(path)])
            self.assertEqual(run.call_args.args[3], marker)
            self.assertEqual(drive.call_args.args[0].publication.mode, 'drive')
            git.assert_not_called()

    def test_native_reuse_requires_the_complete_original_preset(self):
        for changed in ('none', 'symlink-checkout', 'resources', 'logs', 'revision',
                        'checkout', 'binary', 'models', 'provider', 'mode', 'missing-manifest'):
            with self.subTest(changed=changed), directory_fixture.DirectoryRefreshTests().fixture() as f:
                base = f.state
                base.mkdir(mode=0o700)
                (base/'worker.toml').touch()
                manifest = base/'models/manifests/registry.ollama.ai/library/gemma4/31b'
                manifest.parent.mkdir(parents=True)
                raw = b'{"schemaVersion":2,"config":{},"layers":[]}'
                manifest.write_bytes(raw)
                config = replace(f.config,
                    paths=replace(f.config.paths, checkout_dir=(base/'checkout').resolve()),
                    inference=replace(f.config.inference, model=worker.MODEL, revision=hashlib.sha256(raw).hexdigest(),
                        binary=(base/'runtime/ollama').resolve(), models_dir=(base/'models').resolve(),
                        log_dir=(base/'logs').resolve()),
                    resources=replace(f.config.resources, platform='macos', min_available_bytes=32*1024**3, max_load_per_cpu=0.6),
                    publication=PublicationSettings('drive', None, None, 'fixture:', 'folder_1234567890', 'snapshot_1234567890'))
                if changed == 'resources':
                    config = replace(config, resources=replace(config.resources, min_available_bytes=64*1024**3))
                elif changed == 'logs':
                    config = replace(config, inference=replace(config.inference, log_dir=base/'other-logs'))
                elif changed == 'revision':
                    config = replace(config, inference=replace(config.inference, revision='new-weights'))
                elif changed == 'checkout':
                    config = replace(config, paths=replace(config.paths, checkout_dir=f.checkout))
                elif changed == 'symlink-checkout':
                    (base/'checkout').symlink_to(f.checkout, target_is_directory=True)
                    config = replace(config, paths=replace(config.paths, checkout_dir=f.checkout.resolve()))
                elif changed == 'binary':
                    config = replace(config, inference=replace(config.inference, binary=base/'other-ollama'))
                elif changed == 'models':
                    config = replace(config, inference=replace(config.inference, models_dir=base/'other-models'))
                elif changed == 'provider':
                    config = replace(config, inference=replace(config.inference, provider='openai'))
                elif changed == 'mode':
                    config = replace(config, inference=replace(config.inference, mode='external'))
                elif changed == 'missing-manifest':
                    manifest.unlink()
                observed = []
                def run(directory, now, resources, work):
                    observed.append(resources())
                    return {'status': 'idle'}
                with patch.object(worker, 'load_worker_config', return_value=config), \
                        patch.object(worker, 'DriveRefreshJob') as drive, \
                        patch.object(worker, 'RefreshJob') as legacy, \
                        patch.object(worker, 'resource_status', return_value=(True, 'native')), \
                        patch.object(worker, 'configured_resource_status', return_value=(False, 'configured')), \
                        patch.object(worker, 'process_queue', side_effect=run), redirect_stdout(io.StringIO()):
                    worker.main(['--base', str(base)])
                if changed in ('none', 'symlink-checkout'):
                    self.assertEqual(observed, [(True, 'native')])
                    self.assertIn('translation_builder', drive.call_args.kwargs)
                    legacy.assert_called_once_with(base, inference=config.inference)
                else:
                    self.assertEqual(observed, [(False, 'configured')])
                    self.assertNotIn('translation_builder', drive.call_args.kwargs)
                    legacy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
