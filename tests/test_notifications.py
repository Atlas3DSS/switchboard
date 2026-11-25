"""Notification durability regressions, without live services."""
import concurrent.futures
import json
import runpy
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from tests.test_inbox import new_hub, Hub, Store


class NotificationTests(unittest.TestCase):
    def test_full_text_idempotency_and_restart(self):
        h = new_hub()
        message = 'Review this result:\n' + 'long result ' * 100
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: h.notify('a1', message, idem='same-request'), range(8)))
        self.assertEqual(len({r['id'] for r in rows}), 1)
        self.assertEqual(rows[0]['text'], message.strip())
        focus = h.set_focus(inbox=True)
        path = h.s.db.execute('PRAGMA database_list').fetchone()[2]
        h.s.db.close()
        reopened = Hub(Store(path))
        self.assertEqual(reopened.focus, focus)
        self.assertEqual([r['id'] for r in reopened.notifications()['items']], [rows[0]['id']])
        reopened.ack(rows[0]['id'])
        self.assertEqual(reopened.notifications()['items'], [])

    def test_repeat_open_notification_coalesces_but_ack_allows_new(self):
        h = new_hub()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda i: h.notify('a1', 'Review result', idem=f'call-{i}'), range(8)))
        self.assertEqual(len({r['id'] for r in rows}), 1)
        self.assertNotEqual(h.notify('b1', 'Review result')['id'], rows[0]['id'])
        h.ack(rows[0]['id'])
        self.assertNotEqual(h.notify('a1', 'Review result', idem='new')['id'], rows[0]['id'])

    def test_cli_retries_same_body_after_connection_loss(self):
        cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/sb'))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"id":"saved"}'
        with patch('urllib.request.urlopen', side_effect=[urllib.error.URLError('restart'), response]) as request, patch('time.sleep'):
            result = cli['call']('POST', '/api/notifications', {'text': 'hello', 'idem': 'stable'}, retries=1)
        self.assertEqual(result, {'id': 'saved'})
        bodies = [json.loads(c.args[0].data) for c in request.call_args_list]
        self.assertEqual(bodies, [{'text': 'hello', 'idem': 'stable'}] * 2)


if __name__ == '__main__':
    unittest.main()
