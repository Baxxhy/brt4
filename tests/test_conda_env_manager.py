from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt4.runtime import conda_env_manager as envm


def issue(instance_id: str = "django__django-12184") -> dict:
    return {
        "instance_id": instance_id,
        "repo": "django/django",
        "version": "3.1",
        "base_commit": "abc123",
    }


def write_instance(root: Path, instance_id: str, repo_prepare: dict | None = None, summary: dict | None = None) -> None:
    inst = root / instance_id
    inst.mkdir(parents=True, exist_ok=True)
    if repo_prepare is not None:
        (inst / "repo_prepare.json").write_text(json.dumps(repo_prepare), encoding="utf-8")
    if summary is not None:
        (inst / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


class CondaEnvManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def patch_envs(self, inventory: dict[str, str], unhealthy: set[str] | None = None):
        unhealthy = unhealthy or set()

        def fake_health(name: str, timeout: int = 60, refresh: bool = False) -> dict:
            if name not in inventory:
                return {"ok": False, "category": "ENV_NOT_FOUND", "env_name": name}
            if name in unhealthy:
                return {"ok": False, "category": "ENV_INCOMPLETE", "env_name": name, "env_path": inventory[name]}
            return {"ok": True, "category": "", "env_name": name, "env_path": inventory[name], "version": "3.10.0"}

        return mock.patch.multiple(
            envm,
            conda_env_inventory=mock.Mock(return_value=inventory),
            env_health_check=mock.Mock(side_effect=fake_health),
        )

    def test_new_run_uses_recorded_run_prefix_env(self) -> None:
        iid = "django__django-12184"
        recorded = "run_x_setup_django_django__3.1"
        write_instance(self.root, iid, {"env_name": recorded})
        with self.patch_envs({recorded: "/envs/run"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, recorded)
        self.assertEqual(res.resolution_source, "repo_prepare.json:env_name")

    def test_old_run_uses_recorded_unprefixed_env(self) -> None:
        iid = "django__django-12184"
        recorded = "setup_django_django__3.1"
        write_instance(self.root, iid, {"env_name": recorded})
        with self.patch_envs({recorded: "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, recorded)
        self.assertFalse(res.legacy_fallback_used)

    def test_mixed_run_resolves_each_instance_from_own_metadata(self) -> None:
        write_instance(self.root, "django__django-1", {"env_name": "setup_django_django__3.1"})
        write_instance(self.root, "django__django-2", {"env_name": "run_x_setup_django_django__3.1"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/a", "run_x_setup_django_django__3.1": "/envs/b"}):
            a = envm.resolve_eval_env(issue("django__django-1"), str(self.root), run_prefix="run_x_")
            b = envm.resolve_eval_env(issue("django__django-2"), str(self.root), run_prefix="run_x_")
        self.assertEqual(a.resolved_env, "setup_django_django__3.1")
        self.assertEqual(b.resolved_env, "run_x_setup_django_django__3.1")

    def test_environment_nested_metadata_is_used(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"environment": {"env_name": "recorded_env", "status": "READY"}})
        with self.patch_envs({"recorded_env": "/envs/recorded"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root))
        self.assertEqual(res.resolved_env, "recorded_env")

    def test_missing_metadata_uses_legacy_exact_fallback(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="missing_")
        self.assertEqual(res.resolved_env, "setup_django_django__3.1")
        self.assertTrue(res.legacy_fallback_used)

    def test_recorded_missing_env_does_not_fallback(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "missing_recorded"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, "missing_recorded")
        self.assertFalse(res.env_exists)
        self.assertIn("refusing fallback", res.errors[0])

    def test_recorded_env_wins_when_prefix_and_legacy_both_exist(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "setup_django_django__3.1"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy", "run_x_setup_django_django__3.1": "/envs/run"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, "setup_django_django__3.1")

    def test_existing_env_with_failed_health_is_reported(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "bad_env"})
        with self.patch_envs({"bad_env": "/envs/bad"}, unhealthy={"bad_env"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root))
        self.assertTrue(res.env_exists)
        self.assertFalse(res.env_health["ok"])
        self.assertEqual(res.env_health["category"], "ENV_INCOMPLETE")

    def test_setup_interrupted_status_is_extracted(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"environment": {"env_name": "env_a", "status": "creating"}})
        records = envm.extract_env_records(iid, str(self.root))
        self.assertEqual(records[0]["setup_status"], "creating")

    def test_two_workers_request_same_env_deterministically(self) -> None:
        self.assertEqual(
            envm.default_env_name(issue(), prefix="run_x_"),
            envm.default_env_name(issue(), prefix="run_x_"),
        )

    def test_formal_only_uses_generation_metadata(self) -> None:
        self.test_new_run_uses_recorded_run_prefix_env()

    def test_completed_only_uses_generation_metadata(self) -> None:
        self.test_old_run_uses_recorded_unprefixed_env()

    def test_resume_generation_keeps_recorded_identity(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "env_resume"}, {"repo_prepare": {"env_name": "env_other"}})
        records = envm.extract_env_records(iid, str(self.root))
        self.assertEqual(records[0]["env_name"], "env_resume")

    def test_env_name_sanitizes_special_chars_and_long_prefix(self) -> None:
        name = envm.default_env_name({"repo": "owner weird/name weird", "version": "3.1 / x"}, prefix="run " * 50)
        self.assertLessEqual(len(name), 260)
        self.assertNotRegex(name, r"\\s|/")

    def test_preflight_disk_space_failure(self) -> None:
        usage = mock.Mock(total=100, used=99, free=1)
        stat = mock.Mock(f_favail=1)
        with mock.patch.object(envm.shutil, "disk_usage", return_value=usage), mock.patch.object(envm.os, "statvfs", return_value=stat), mock.patch.object(envm.Path, "exists", return_value=True):
            data = envm.preflight_system([str(self.root)], min_free_gb=1, min_free_inodes=10)
        self.assertFalse(data["ok"])

    def test_conda_nonzero_health_is_incomplete(self) -> None:
        with self.patch_envs({"env_a": "/envs/a"}, unhealthy={"env_a"}):
            health = envm.env_health_check("env_a")
        self.assertFalse(health["ok"])
        self.assertEqual(health["category"], "ENV_INCOMPLETE")

    def test_no_suffix_fallback_to_other_run_env(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {})
        with self.patch_envs({"other_run_setup_django_django__3.1": "/envs/other"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertFalse(res.env_exists)
        self.assertNotEqual(res.resolved_env, "other_run_setup_django_django__3.1")


if __name__ == "__main__":
    unittest.main()
