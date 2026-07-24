from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analyzer import AnalyzerSession, build_call_view
from runtime_model import build_runtime_objects


class RuntimeModelTests(unittest.TestCase):
    def test_freertos_tasks_timers_main_and_isr_are_execution_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.c").write_text(
                "typedef void* TaskHandle_t;\n"
                "void CommTask(void *arg){for(;;){}}\n"
                "void HealthTimer(void *timer){}\n"
                "void USART1_IRQHandler(void){}\n"
                "int main(void){\n"
                '  xTaskCreate(CommTask, "COMM", 512, 0, 3, &commHandle);\n'
                '  xTimerCreate("HEALTH", 1000, 1, 0, HealthTimer);\n'
                "  return 0;\n"
                "}\n",
                encoding="utf-8",
            )
            result = AnalyzerSession().initial_scan(str(root))
            objects = build_runtime_objects(result)
            by_kind = {(item.kind, item.name): item for item in objects}
            self.assertIn(("main", "main"), by_kind)
            self.assertIn(("task", "CommTask"), by_kind)
            self.assertIn(("timer", "HealthTimer"), by_kind)
            self.assertIn(("isr", "USART1_IRQHandler"), by_kind)
            self.assertEqual(by_kind[("task", "CommTask")].attributes["Priority"], "3")
            self.assertEqual(by_kind[("task", "CommTask")].attributes["Stack"], "512")
            view = build_call_view(result)
            self.assertEqual(view.runtime_roots, 2)
            runtime_sections = [
                row for row in view.rows
                if row.kind == "section" and row.state == "runtime"
            ]
            self.assertEqual(len(runtime_sections), 2)


if __name__ == "__main__":
    unittest.main()
