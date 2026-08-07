import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zhiyuan_bench.cli import _parser
from zhiyuan_bench.web import WebConfig, run_web_server


class CliTests(unittest.TestCase):
    def test_ui_accepts_one_click_browser_option(self) -> None:
        args = _parser().parse_args(
            [
                "ui",
                "--repo",
                "repo",
                "--workspace",
                "workspace",
                "--open-browser",
            ]
        )

        self.assertTrue(args.open_browser)

    def test_web_server_schedules_browser_open(self) -> None:
        uvicorn = types.SimpleNamespace(run=Mock())
        config = WebConfig(Path("repo"), Path("workspace"), Path("records"))
        with (
            patch.dict(sys.modules, {"uvicorn": uvicorn}),
            patch("zhiyuan_bench.web.threading.Timer") as timer,
        ):
            run_web_server(config, host="0.0.0.0", port=9876, open_browser=True)

        self.assertEqual(timer.call_args.args[0], 0.75)
        self.assertEqual(timer.call_args.kwargs["args"], ("http://127.0.0.1:9876/",))
        timer.return_value.start.assert_called_once_with()
        uvicorn.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
