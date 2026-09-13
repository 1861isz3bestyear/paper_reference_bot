import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from reference_bot.cli import PaperBot
from reference_bot.trading import LocalPosition
from shared.reference_target import config_fingerprint, publish_target
from bybit_demo_bot.test_bybit_demo import config


def test_target_contains_actual_ledger_position_and_identity(tmp_path):
    cfg = config(ticker='XRP_USDT')
    state = SimpleNamespace(launched_at='2026-09-13T10:58:55+00:00',
                            last_processed_candle='2026-09-13T11:00:00+00:00')
    position = LocalPosition('XRPUSDT', 'Short', Decimal('7.4'), 1.34,
                             '2026-09-13T11:00:04+00:00')
    path = tmp_path / 'reference_target.json'
    publish_target(path, cfg, state, position)
    data = json.loads(path.read_text())
    assert data['quantity'] == '7.4'
    assert data['entry_price'] == 1.34
    assert data['position_id'] == position.updated_at
    assert data['config_fingerprint'] == config_fingerprint(cfg)
    assert datetime.fromisoformat(data['published_at']).tzinfo is not None
    assert not path.with_suffix('.json.tmp').exists()
    publish_target(path, cfg, state, LocalPosition('XRPUSDT', None, Decimal('0'), None, None))
    assert json.loads(path.read_text())['position_id'] is None


@pytest.mark.parametrize('fails', [False, True])
def test_runner_only_publishes_after_successful_reconciliation(tmp_path, fails):
    bot = PaperBot.__new__(PaperBot)
    bot.config = config(ticker='XRP_USDT')
    bot.state = SimpleNamespace(launched_at=datetime.now(timezone.utc).isoformat(),
                                last_processed_candle=None)
    bot.state_path = tmp_path / 'reference_state.json'
    bot.quote_currency = 'USDT'
    bot.trade_symbol = 'XRPUSDT'
    bot.market_feed = Mock()
    bot.trader = Mock()
    bot.trader.current_position.return_value = LocalPosition('XRPUSDT', None, Decimal('0'), None, None)
    bot.running = True
    bot.process_available_candles = Mock(side_effect=RuntimeError('history incomplete') if fails else None)
    bot._wait = lambda _: setattr(bot, 'running', False)
    bot.run_forever()
    assert (tmp_path / 'reference_target.json').exists() is not fails
    bot.market_feed.stop.assert_called_once()
