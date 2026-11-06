"""Source/release compatibility without starting commands or creating a release."""
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPOSITORY=Path(os.environ.get('ASCEND_LAB_REPOSITORY_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(REPOSITORY/'src/ascend_lab'))
from source_layout import COMMANDS, project_root, source_file, state_root, release_bytes, public_paths


class RepositoryLayoutTests(unittest.TestCase):
    def test_root_is_small_and_all_commands_have_real_source_targets(self):
        self.assertEqual(list(REPOSITORY.glob('*.py')),[])
        for name in COMMANDS:
            self.assertTrue(os.access(REPOSITORY/'bin'/name,os.X_OK))
            self.assertTrue(os.access(source_file(REPOSITORY,name),os.X_OK))
            self.assertIn('src/ascend_lab/'+name,(REPOSITORY/'bin'/name).read_text())

    def test_logical_role_files_still_resolve_and_no_private_state_is_public(self):
        import deployment
        paths=set(public_paths(REPOSITORY))
        for name in deployment.CORE:
            selected=source_file(REPOSITORY,name)
            self.assertTrue(selected.is_file(),name)
            self.assertIn(selected,paths,name)
        for path in paths:
            self.assertFalse({'runtime','tools','.git','__pycache__'}&set(path.relative_to(REPOSITORY).parts))
            self.assertNotEqual(path.name,'AGENTS.md')

    def test_repository_state_is_preserved_but_nested_personal_clients_do_not_inherit_it(self):
        self.assertEqual(state_root(REPOSITORY/'src/ascend_lab'),REPOSITORY/'runtime')
        nested=REPOSITORY/'runtime'/'some-personal-client'
        self.assertEqual(state_root(nested),nested/'runtime')

    def test_flat_installed_layout_needs_no_repository_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'jobs.py').write_text('fixture')
            self.assertEqual(source_file(root,'jobs.py'),root/'jobs.py')
            self.assertEqual(project_root(root),root)
            self.assertEqual(state_root(root),root/'runtime')
            with self.assertRaises(ValueError):source_file(root,'../private')

    def test_release_docs_restore_flat_links_and_source_commands(self):
        text=release_bytes(REPOSITORY,'README.md').decode()
        self.assertIn('](CORE_OPERATIONS.md)',text)
        self.assertNotIn('./bin/labctl',text)
        self.assertIn('./labctl',text)

    def test_repository_assets_and_compiler_driver_use_source_layout(self):
        root=REPOSITORY/'src/ascend_lab'
        for asset in ['web/app.js','examples/cpu_task.py','examples/isolation_probe.py']:
            self.assertEqual(source_file(root,asset),REPOSITORY/asset)
        import compiler_experiment
        with patch.object(compiler_experiment,'__file__',str(root/'compiler_experiment.py')):
            self.assertEqual(compiler_experiment.driver_bytes(),
                             (REPOSITORY/'examples/workflows'/compiler_experiment.DRIVER).read_bytes())

    def test_packaged_core_has_no_dangling_local_document_links(self):
        import deployment,re
        files=deployment.bundle_sources(REPOSITORY)
        self.assertTrue(all(not name.startswith(('src/','docs/','bin/','runtime/')) for name in files))
        for name in files:
            if not name.endswith('.md'):continue
            for target in re.findall(r'\]\(([^\s)#]+)',release_bytes(REPOSITORY,name).decode()):
                if target.endswith('.md') and '/' not in target:
                    self.assertIn(target,files,(name,target))

    def test_all_source_entrypoints_parse_and_installer_has_layout_helper(self):
        for path in (REPOSITORY/'src/ascend_lab').glob('*.py'):
            ast.parse(path.read_text(),filename=str(path))
        for name in COMMANDS:
            path=source_file(REPOSITORY,name)
            if name!='agentctl':ast.parse(path.read_text(),filename=str(path))
        installer=source_file(REPOSITORY,'install-client').read_text()
        self.assertIn("('source_layout.py','source_layout.py')",installer)
        self.assertIn('release_bytes(root,source)',installer)


if __name__=='__main__':
    unittest.main()
