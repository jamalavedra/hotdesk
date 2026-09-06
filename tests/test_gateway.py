import unittest
from unittest.mock import patch

import httpx

from desktop.gateway import app


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_rejects_malformed_headers(self):
        with patch.dict("os.environ", {"HOTDESK_GATEWAY_TOKEN": "test-secret"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                for authorization in (b"", b"Bearer wrong", b"Bearer \xff"):
                    response = await client.get(
                        "/computer/status", headers={b"authorization": authorization}
                    )
                    self.assertEqual(response.status_code, 401)
                response = await client.get(
                    "/unknown", headers={"authorization": "Bearer test-secret"}
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual((await client.get("/health")).status_code, 200)
