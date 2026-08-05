import json
import subprocess
import unittest
from collections.abc import Sequence

from zhiyuan_bench.containers import ContainerTracker


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        value = list(command)
        self.commands.append(value)
        if "ps" in value:
            return subprocess.CompletedProcess(value, 0, "old\nclean\nmounted\n", "")
        if "inspect" in value:
            container_id = value[value.index("inspect") + 1]
            mounts = [{"Source": "user-data"}] if container_id == "mounted" else []
            return subprocess.CompletedProcess(value, 0, json.dumps(mounts), "")
        return subprocess.CompletedProcess(value, 0, "", "")


class ContainerTrackerTests(unittest.TestCase):
    def test_cleanup_only_removes_new_unmounted_containers(self) -> None:
        docker = FakeDocker()
        tracker = ContainerTracker("ssh://test", run_command=docker)
        removed, skipped = tracker.cleanup_created({"old"})
        self.assertEqual(removed, ["clean"])
        self.assertEqual(skipped, ["mounted"])
        stop_commands = [command for command in docker.commands if "stop" in command]
        rm_commands = [command for command in docker.commands if "rm" in command]
        self.assertEqual(stop_commands[0][-1], "clean")
        self.assertEqual(rm_commands[0][-1], "clean")
        self.assertFalse(any(command[-1] == "old" for command in stop_commands))
        self.assertFalse(any(command[-1] == "mounted" for command in stop_commands))


if __name__ == "__main__":
    unittest.main()
