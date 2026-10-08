import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_desktop import budget_passed, cpu_delta, process_metrics, summarize


class BenchmarkTests(unittest.TestCase):
    def test_windows_private_bytes_do_not_need_full_memory_query(self):
        process = Mock(pid=7)
        process.children.return_value = []
        process.memory_info.return_value = SimpleNamespace(private=100, rss=140)
        process.cpu_times.return_value = SimpleNamespace(user=1, system=2)
        process.name.return_value = "desktop"
        with patch("benchmark_desktop.psutil.Process", return_value=process):
            result = process_metrics(7, {})
        self.assertEqual(result["private_bytes"], 100)
        process.memory_full_info.assert_not_called()

    def test_uses_total_machine_cpu_and_whole_tree_memory(self):
        samples = [
            {"private_bytes": 100 * 2**20, "working_set_bytes": 140 * 2**20,
             "cpu_delta": .16, "processes": [{"name": "controller"}]},
            {"private_bytes": 120 * 2**20, "working_set_bytes": 160 * 2**20,
             "cpu_delta": .16, "processes": [{"name": "webview"}]},
        ]
        result = summarize(samples, 8, 2)
        self.assertEqual(result["private_mib_mean"], 110)
        self.assertEqual(result["private_mib_peak"], 120)
        self.assertEqual(result["machine_cpu_percent"], 2)
        self.assertEqual(result["process_names"], ["controller", "webview"])

    def test_rejects_invalid_measurement(self):
        with self.assertRaises(ValueError):
            summarize([], 8, 2)

    def test_cpu_counts_new_children_without_old_startup_usage(self):
        child = Mock(pid=8)
        child.cpu_times.return_value = SimpleNamespace(user=.2, system=.1)
        child.create_time.return_value = 101
        previous = {}
        self.assertAlmostEqual(cpu_delta(child, previous, 100), .3)
        child.cpu_times.return_value = SimpleNamespace(user=.3, system=.1)
        self.assertAlmostEqual(cpu_delta(child, previous, 100), .1)
        older = Mock(pid=9)
        older.cpu_times.return_value = SimpleNamespace(user=4, system=2)
        older.create_time.return_value = 99
        self.assertEqual(cpu_delta(older, previous, 100), 0)

    def test_idle_peak_must_fit_budget_even_when_mean_fits(self):
        result = {"private_mib_mean": 240, "private_mib_peak": 251,
                  "machine_cpu_percent": .5, "opening_seconds": 2}
        self.assertFalse(budget_passed(result))
        result["private_mib_peak"] = 250
        self.assertTrue(budget_passed(result))


if __name__ == "__main__":
    unittest.main()
