import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class LifecycleSnapshotTest(unittest.TestCase):
    @staticmethod
    def request():
        return {
            "id": "life01",
            "hostname": "life01",
            "status": "completed",
            "requested_by": "user",
            "resources": {"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            "additional_disks": [],
            "events": [],
        }

    @patch.object(app, "local_qemu_domain_state", side_effect=["running", "shut off"])
    @patch.object(app.subprocess, "run")
    def test_graceful_stop_is_audited(self, run, _state):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        request = self.request()
        success, state = app.lifecycle_action(request, "stop", "user")
        self.assertTrue(success)
        self.assertEqual(state, "shut off")
        self.assertEqual(request["events"][-1]["name"], "VM stop completed")
        self.assertIn("shutdown", run.call_args.args[0])

    @patch.object(app, "domain_disk_files")
    @patch.object(app.subprocess, "run")
    def test_resize_snapshot_records_overlay_and_previous_resources(self, run, disks):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-snapshot-test-"))
        base = directory / "life01.qcow2"
        base.touch()
        disks.return_value = [{"target": "vda", "source": str(base)}]
        run.return_value.returncode = 0
        run.return_value.stdout = "<domain><devices/></domain>"
        run.return_value.stderr = ""
        request = self.request()
        with patch.dict(app.LOCAL_QEMU, {"storage_path": str(directory), "uri": "qemu:///system"}):
            snapshot, error = app.create_resize_snapshot(
                request,
                {"vcpu": 4, "memory_gb": 8, "disk_gb": 80},
                [],
            )
        self.assertEqual(error, "")
        self.assertEqual(snapshot["status"], "active")
        self.assertEqual(snapshot["previous_resources"]["memory_gb"], 4)
        self.assertEqual(snapshot["disks"][0]["target"], "vda")
        self.assertEqual(request["events"][-1]["name"], "Pre-resize snapshot created")

    @patch.object(app, "local_qemu_domain_state", return_value="shut off")
    @patch.object(app.subprocess, "run")
    def test_rollback_keeps_overlay_as_recovery_file(self, run, _state):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-rollback-test-"))
        overlay = directory / "life01-snapshot-vda.qcow2"
        overlay.touch()
        xml_path = directory / "life01-before.xml"
        xml_path.write_text("<domain/>", encoding="utf-8")
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        request = self.request()
        request["resources"] = {"vcpu": 4, "memory_gb": 8, "disk_gb": 80}
        request["resize_snapshots"] = [{
            "id": "snapshot01",
            "status": "active",
            "xml_path": str(xml_path),
            "disks": [{"target": "vda", "base": "base.qcow2", "overlay": str(overlay)}],
            "previous_resources": {"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            "previous_additional_disks": [],
        }]
        success, error = app.rollback_resize_snapshot(
            request, "snapshot01", "admin"
        )
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertFalse(overlay.exists())
        recovery = Path(request["resize_snapshots"][0]["recovery_files"][0]["to"])
        self.assertTrue(recovery.exists())
        self.assertIn(".rolled-back-", recovery.name)
        self.assertEqual(request["resources"]["memory_gb"], 4)

    @patch.object(app, "local_qemu_domain_state", return_value="running")
    @patch.object(app, "domain_disk_files")
    @patch.object(app.subprocess, "run")
    def test_manual_snapshot_records_owner_and_consistency(self, run, disks, _state):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-manual-snapshot-test-"))
        base = directory / "life01.qcow2"
        base.touch()
        disks.return_value = [{"target": "vda", "source": str(base)}]
        run.return_value.returncode = 0
        run.return_value.stdout = (
            "<domain><devices><disk device='disk'><source file='"
            f"{base}'/><target dev='vda'/></disk></devices></domain>"
        )
        run.return_value.stderr = ""
        request = self.request()
        with patch.dict(app.LOCAL_QEMU, {"storage_path": str(directory), "uri": "qemu:///system"}):
            snapshot, error = app.create_manual_snapshot(
                request, "Before upgrade", "Known good state", "user"
            )
        self.assertEqual(error, "")
        self.assertEqual(snapshot["name"], "Before upgrade")
        self.assertEqual(snapshot["created_by"], "user")
        self.assertEqual(snapshot["consistency"], "application-consistent")
        self.assertEqual(request["events"][-1]["name"], "Manual snapshot created")
        self.assertIn("--quiesce", run.call_args.args[0])

    @patch.object(app, "local_qemu_domain_state", return_value="shut off")
    @patch.object(app, "domain_disk_files")
    @patch.object(app.subprocess, "run")
    def test_manual_restore_creates_new_overlay_branch(self, run, disks, _state):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-manual-restore-test-"))
        base = directory / "life01.qcow2"
        base.touch()
        xml_path = directory / "life01-manual.xml"
        xml_path.write_text(
            "<domain><devices><disk device='disk'><source file='"
            f"{base}'/><target dev='vda'/></disk></devices></domain>",
            encoding="utf-8",
        )
        def run_command(command, **_kwargs):
            result = type("Result", (), {
                "returncode": 0,
                "stdout": "<domain/>" if "dumpxml" in command else "",
                "stderr": "",
            })()
            return result

        run.side_effect = run_command
        disks.side_effect = lambda _hostname: [{
            "target": "vda",
            "source": next(
                argument.split("file=", 1)[1].split(",", 1)[0]
                for call in run.call_args_list
                for argument in call.args[0]
                if isinstance(argument, str) and argument.startswith("vda,file=")
            ),
        }]
        request = self.request()
        request["resources"] = {"vcpu": 4, "memory_gb": 8, "disk_gb": 80}
        request["manual_snapshots"] = [{
            "id": "manual01",
            "name": "Before upgrade",
            "status": "available",
            "xml_path": str(xml_path),
            "disks": [{"target": "vda", "base": str(base), "overlay": "old-overlay.qcow2"}],
            "resources": {"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            "additional_disks": [],
            "restores": [],
        }]
        with patch.dict(app.LOCAL_QEMU, {"storage_path": str(directory), "uri": "qemu:///system"}):
            success, error = app.restore_manual_snapshot(request, "manual01", "user")
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(request["resources"]["memory_gb"], 4)
        restore = request["manual_snapshots"][0]["restores"][0]
        self.assertEqual(restore["restored_by"], "user")
        self.assertIn("-restore-", restore["overlays"][0]["overlay"])
        self.assertEqual(request["events"][-1]["name"], "Manual snapshot restored")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any(command[0] == "qemu-img" for command in commands))
        snapshot_command = next(
            command for command in commands if "snapshot-create-as" in command
        )
        self.assertIn("--atomic", snapshot_command)
        self.assertIn("--no-metadata", snapshot_command)

    @patch.object(app, "local_qemu_domain_state", return_value="shut off")
    @patch.object(app.subprocess, "run")
    def test_manual_restore_restores_previous_definition_on_failure(self, run, _state):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-manual-restore-failure-test-"))
        base = directory / "life01.qcow2"
        base.touch()
        xml_path = directory / "life01-manual.xml"
        xml_path.write_text(
            "<domain><devices><disk device='disk'><source file='"
            f"{base}'/><target dev='vda'/></disk></devices></domain>",
            encoding="utf-8",
        )
        previous_xml = "<domain><name>life01</name><devices/></domain>"

        def run_command(command, **kwargs):
            if "dumpxml" in command:
                return type("Result", (), {
                    "returncode": 0, "stdout": previous_xml, "stderr": "",
                })()
            if "snapshot-create-as" in command:
                return type("Result", (), {
                    "returncode": 1, "stdout": "", "stderr": "snapshot failed",
                })()
            return type("Result", (), {
                "returncode": 0, "stdout": "", "stderr": "",
            })()

        run.side_effect = run_command
        request = self.request()
        request["manual_snapshots"] = [{
            "id": "manual01",
            "name": "Before upgrade",
            "status": "available",
            "xml_path": str(xml_path),
            "disks": [{"target": "vda", "base": str(base), "overlay": "old-overlay.qcow2"}],
            "resources": {"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            "additional_disks": [],
            "restores": [],
        }]
        with patch.dict(app.LOCAL_QEMU, {"storage_path": str(directory), "uri": "qemu:///system"}):
            success, error = app.restore_manual_snapshot(request, "manual01", "user")
        self.assertFalse(success)
        self.assertEqual(error, "snapshot failed")
        define_calls = [
            call for call in run.call_args_list if "define" in call.args[0]
        ]
        self.assertEqual(len(define_calls), 2)
        self.assertEqual(define_calls[-1].kwargs["input"], previous_xml)

    def test_manual_snapshot_delete_plan_lists_exact_metadata_target(self):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-delete-plan-test-"))
        xml_path = directory / "life01-manual.xml"
        xml_path.write_text("<domain/>", encoding="utf-8")
        overlay = directory / "life01-manual-vda.qcow2"
        request = self.request()
        request["manual_snapshots"] = [{
            "id": "manual01",
            "name": "Before upgrade",
            "status": "available",
            "xml_path": str(xml_path),
            "disks": [{"target": "vda", "base": "base.qcow2", "overlay": str(overlay)}],
            "restores": [],
        }]
        plan, error = app.manual_snapshot_delete_plan(request, "manual01")
        self.assertEqual(error, "")
        self.assertEqual(plan["delete_target_count"], 1)
        self.assertEqual(plan["delete_targets"], [str(xml_path)])
        self.assertEqual(plan["retained_disk_files"], [str(overlay)])

    def test_delete_plan_route_accepts_generated_snapshot_id(self):
        snapshot_id = "manual-20260726T142642Z-0bb5e0"
        match = app.SNAPSHOT_DELETE_PLAN_PATH.fullmatch(
            f"/api/requests/7a425c0d/snapshots/{snapshot_id}/delete-plan"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), ("7a425c0d", snapshot_id))

    def test_resize_snapshot_expires_after_twelve_hours(self):
        request = self.request()
        request["resize_snapshots"] = [{
            "id": "resize-expiring",
            "created_at": "2026-07-26T00:00:00+00:00",
            "status": "active",
        }]
        changed = app.expire_resize_snapshots(
            [request], app.datetime(2026, 7, 26, 12, 0, 1, tzinfo=app.UTC)
        )
        self.assertTrue(changed)
        self.assertEqual(request["resize_snapshots"][0]["status"], "expired")
        self.assertEqual(request["events"][-1]["name"], "Resize snapshot expired")

    @patch.object(app, "domain_disk_files")
    def test_expired_resize_cleanup_plan_lists_exact_targets(self, disks):
        directory = Path(tempfile.mkdtemp(prefix="vm-foundry-resize-cleanup-plan-test-"))
        base = directory / "life01.qcow2"
        overlay = directory / "life01-resize.qcow2"
        xml_path = directory / "life01-resize.xml"
        overlay.touch()
        xml_path.touch()
        disks.return_value = [{"target": "vda", "source": str(overlay)}]
        request = self.request()
        request["resize_snapshots"] = [{
            "id": "resize-expired",
            "status": "expired",
            "xml_path": str(xml_path),
            "disks": [{
                "target": "vda",
                "base": str(base),
                "overlay": str(overlay),
            }],
        }]
        plan, error = app.resize_snapshot_cleanup_plan(request, "resize-expired")
        self.assertEqual(error, "")
        self.assertEqual(plan["pending_commit_disks"], ["vda"])
        self.assertEqual(plan["delete_targets"], [str(overlay), str(xml_path)])
        self.assertEqual(plan["delete_target_count"], 2)


if __name__ == "__main__":
    unittest.main()
