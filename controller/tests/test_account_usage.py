from types import SimpleNamespace
from unittest.mock import Mock

from controller.core import Controller
from controller.state import AppError
from controller.tests.test_controller import TemporaryStore


class AccountUsageTests(TemporaryStore):
    def setUp(self):
        super().setUp()
        self.colab = Mock()
        self.colab.client.get_consumption_user_info.return_value = SimpleNamespace(paid_compute_units_balance=0.0, consumption_rate_hourly=0.0)
        self.controller = Controller(self.store, auth=Mock(), colab_factory=lambda *_: self.colab)

    def tearDown(self):
        self.controller.close()
        super().tearDown()

    def read(self, force=False):
        return self.controller.dispatch({"id": "usage", "action": "account.usage", "params": {"account_id": self.account_id, "force": force}})

    def test_idle_balance_zero_is_real_and_cached_without_any_allocation(self):
        result = self.read()
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["balance"], 0)
        self.assertEqual(result["result"]["status"], "ready")
        self.assertIsNotNone(result["result"]["checked_at"])
        self.read()
        self.colab.client.get_consumption_user_info.assert_called_once()
        self.read(force=True)
        self.assertEqual(self.colab.client.get_consumption_user_info.call_count, 2)
        self.assertEqual(self.controller.snapshot()["metrics"]["balance"], 0)
        self.assertEqual(self.controller.session["state"], "idle")
        self.colab.allocate.assert_not_called()
        self.colab.recover.assert_not_called()
        self.colab.stop.assert_not_called()

    def test_account_switch_during_request_cannot_display_previous_balance(self):
        other = self.store.add("Other")
        self.store.bind(other, {**self.identity, "sub": "subject-b"})
        def switch():
            self.store.set("selected_account_id", other)
            return SimpleNamespace(paid_compute_units_balance=999.0, consumption_rate_hourly=2.0)
        self.colab.client.get_consumption_user_info.side_effect = switch
        self.read()
        self.assertIsNone(self.controller.snapshot()["metrics"]["balance"])
        self.assertEqual(self.controller.snapshot()["account_usage"]["account_id"], other)

    def test_failed_refresh_does_not_keep_old_balance_or_expose_server_error(self):
        self.read()
        self.colab.client.get_consumption_user_info.side_effect = RuntimeError("secret-token-and-url")
        result = self.read(force=True)
        self.assertEqual(result["result"]["status"], "error")
        self.assertEqual(result["result"]["error_code"], "balance_unavailable")
        self.assertIsNone(self.controller.snapshot()["metrics"]["balance"])
        self.assertNotIn("secret-token-and-url", str(result))
        self.read()
        self.assertEqual(self.colab.client.get_consumption_user_info.call_count, 2)

    def test_identity_failure_and_invalid_numbers_are_not_balances(self):
        for value in [float("nan"), float("inf"), True, -1, "50"]:
            self.colab.client.get_consumption_user_info.return_value = SimpleNamespace(paid_compute_units_balance=value, consumption_rate_hourly=0)
            self.assertEqual(self.read(force=True)["result"]["status"], "error")
        self.controller.colab_factory = Mock(side_effect=AppError("account_mismatch", "private"))
        self.assertEqual(self.read(force=True)["result"]["error_code"], "account_mismatch")

    def test_removed_account_and_benchmark_never_query_cloud(self):
        self.controller.benchmark = True
        self.assertEqual(self.read()["error"]["code"], "benchmark_readonly")
        self.controller.benchmark = False
        self.store.remove(self.account_id)
        self.assertEqual(self.read()["error"]["code"], "account_missing")
        self.colab.client.get_consumption_user_info.assert_not_called()

    def test_read_does_not_take_lifecycle_lock_and_duplicate_read_is_bounded(self):
        with self.controller.operation:
            self.assertTrue(self.read()["ok"])
        with self.controller.usage_locks[self.account_id]:
            self.assertEqual(self.read(force=True)["result"]["status"], "ready")
        self.colab.client.get_consumption_user_info.assert_called_once()

    def test_one_pending_account_does_not_block_another_account_balance(self):
        self.read()
        other = self.store.add("Other")
        self.store.bind(other, {**self.identity, "sub": "subject-b"})
        with self.controller.usage_locks[self.account_id]:
            result = self.controller.read_usage(other)
        self.assertEqual(result["account_id"], other)
        self.assertEqual(result["status"], "ready")
