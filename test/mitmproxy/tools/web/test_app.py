import gzip
import importlib
import json
import logging
from pathlib import Path
from unittest import mock

import mitmproxy_rs
import pytest
import tornado.testing
from tornado import httpclient
from tornado import websocket

from mitmproxy import log
from mitmproxy import options
from mitmproxy.test import tflow
from mitmproxy.tools.web import app
from mitmproxy.tools.web import master as webmaster

here = Path(__file__).parent.absolute()


@pytest.fixture(scope="module")
def no_tornado_logging():
    logging.getLogger("tornado.access").disabled = True
    logging.getLogger("tornado.application").disabled = True
    logging.getLogger("tornado.general").disabled = True
    yield
    logging.getLogger("tornado.access").disabled = False
    logging.getLogger("tornado.application").disabled = False
    logging.getLogger("tornado.general").disabled = False


def get_json(resp: httpclient.HTTPResponse):
    return json.loads(resp.body.decode())


@pytest.mark.parametrize("filename", list((here / "../../../../web/gen").glob("*.py")))
async def test_generated_files(filename):
    mod = importlib.import_module(f"web.gen.{filename.stem}")
    expected = await mod.make()
    actual = mod.filename.read_text().replace("\r\n", "\n")
    assert (
        actual == expected
    ), f"{mod.filename} must be regenerated by running {filename.resolve()}."


@pytest.mark.usefixtures("no_tornado_logging", "tdata")
class TestApp(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        async def make_master() -> webmaster.WebMaster:
            o = options.Options(http2=False)
            return webmaster.WebMaster(o, with_termlog=False)

        m: webmaster.WebMaster = self.io_loop.asyncio_loop.run_until_complete(
            make_master()
        )
        f = tflow.tflow(resp=True)
        f.id = "42"
        f.request.content = b"foo\nbar"
        f2 = tflow.tflow(ws=True, resp=True)
        f2.request.content = None
        f2.response.content = None
        f2.id = "43"
        m.view.add([f, f2])
        m.view.add([tflow.tflow(err=True)])
        m.events._add_log(log.LogEntry("test log", "info"))
        m.events.done()
        self.master = m
        self.view = m.view
        self.events = m.events
        webapp = app.Application(m, None)
        webapp.settings["xsrf_cookies"] = False
        return webapp

    def fetch(self, *args, **kwargs) -> httpclient.HTTPResponse:
        # tornado disallows POST without content by default.
        return super().fetch(*args, **kwargs, allow_nonstandard_methods=True)

    def put_json(self, url, data: dict) -> httpclient.HTTPResponse:
        return self.fetch(
            url,
            method="PUT",
            body=json.dumps(data),
            headers={"Content-Type": "application/json"},
        )

    def test_index(self):
        response: httpclient.HTTPResponse = self.fetch("/")
        assert response.code == 200
        assert '"/' not in str(
            response.body
        ), "HTML content should not contain root-relative paths"

    def test_filter_help(self):
        assert self.fetch("/filter-help").code == 200

    def test_flows(self):
        resp = self.fetch("/flows")
        assert resp.code == 200
        assert get_json(resp)[0]["request"]["contentHash"]
        assert get_json(resp)[2]["error"]

    def test_flows_dump(self):
        resp = self.fetch("/flows/dump")
        assert b"address" in resp.body

    def test_flows_dump_filter(self):
        resp = self.fetch("/flows/dump?filter=foo")
        assert b"" == resp.body

    def test_flows_dump_filter_error(self):
        resp = self.fetch("/flows/dump?filter=[")
        assert resp.code == 400

    def test_clear(self):
        events = self.events.data.copy()
        flows = list(self.view)

        assert self.fetch("/clear", method="POST").code == 200

        assert not len(self.view)
        assert not len(self.events.data)

        # restore
        for f in flows:
            self.view.add([f])
        self.events.data = events

    def test_resume(self):
        for f in self.view:
            f.intercept()

        assert self.fetch("/flows/42/resume", method="POST").code == 200
        assert sum(f.intercepted for f in self.view) >= 1
        assert self.fetch("/flows/resume", method="POST").code == 200
        assert all(not f.intercepted for f in self.view)

    def test_kill(self):
        for f in self.view:
            f.backup()
            f.intercept()

        assert self.fetch("/flows/42/kill", method="POST").code == 200
        assert sum(f.killable for f in self.view) >= 1
        assert self.fetch("/flows/kill", method="POST").code == 200
        assert all(not f.killable for f in self.view)
        for f in self.view:
            f.revert()

    def test_flow_delete(self):
        f = self.view.get_by_id("42")
        assert f

        assert self.fetch("/flows/42", method="DELETE").code == 200

        assert not self.view.get_by_id("42")
        self.view.add([f])

        assert self.fetch("/flows/1234", method="DELETE").code == 404

    def test_flow_update(self):
        f = self.view.get_by_id("42")
        assert f.request.method == "GET"
        f.backup()

        upd = {
            "request": {
                "method": "PATCH",
                "port": 123,
                "headers": [("foo", "bar")],
                "trailers": [("foo", "bar")],
                "content": "req",
            },
            "response": {
                "msg": "Non-Authorisé",
                "code": 404,
                "headers": [("bar", "baz")],
                "trailers": [("foo", "bar")],
                "content": "resp",
            },
            "marked": ":red_circle:",
            "comment": "I'm a modified comment!",
        }
        assert self.put_json("/flows/42", upd).code == 200
        assert f.request.method == "PATCH"
        assert f.request.port == 123
        assert f.request.headers["foo"] == "bar"
        assert f.request.text == "req"
        assert f.response.msg == "Non-Authorisé"
        assert f.response.status_code == 404
        assert f.response.headers["bar"] == "baz"
        assert f.response.text == "resp"
        assert f.comment == "I'm a modified comment!"

        upd = {
            "request": {
                "trailers": [("foo", "baz")],
            },
            "response": {
                "trailers": [("foo", "baz")],
            },
        }
        assert self.put_json("/flows/42", upd).code == 200
        assert f.request.trailers["foo"] == "baz"

        f.revert()

        assert self.put_json("/flows/42", {"foo": 42}).code == 400
        assert self.put_json("/flows/42", {"request": {"foo": 42}}).code == 400
        assert self.put_json("/flows/42", {"response": {"foo": 42}}).code == 400
        assert self.fetch("/flows/42", method="PUT", body="{}").code == 400
        assert (
            self.fetch(
                "/flows/42",
                method="PUT",
                headers={"Content-Type": "application/json"},
                body="!!",
            ).code
            == 400
        )

    def test_flow_duplicate(self):
        resp = self.fetch("/flows/42/duplicate", method="POST")
        assert resp.code == 200
        f = self.view.get_by_id(resp.body.decode())
        assert f
        assert f.id != "42"
        self.view.remove([f])

    def test_flow_revert(self):
        f = self.view.get_by_id("42")
        f.backup()
        f.request.method = "PATCH"
        self.fetch("/flows/42/revert", method="POST")
        assert not f._backup

    def test_flow_replay(self):
        with mock.patch("mitmproxy.command.CommandManager.call") as replay_call:
            assert self.fetch("/flows/42/replay", method="POST").code == 200
            assert replay_call.called

    def test_flow_content(self):
        f = self.view.get_by_id("42")
        f.backup()
        f.response.headers["Content-Disposition"] = 'inline; filename="filename.jpg"'

        r = self.fetch("/flows/42/response/content.data")
        assert r.body == b"message"
        assert r.headers["Content-Disposition"] == 'attachment; filename="filename.jpg"'

        del f.response.headers["Content-Disposition"]
        f.request.path = "/foo/bar.jpg"
        assert (
            self.fetch("/flows/42/response/content.data").headers["Content-Disposition"]
            == "attachment; filename=bar.jpg"
        )

        f.response.content = b""
        r = self.fetch("/flows/42/response/content.data")
        assert r.code == 200
        assert r.body == b""

        f.revert()

    def test_flow_content_returns_raw_content_when_decoding_fails(self):
        f = self.view.get_by_id("42")
        f.backup()

        f.response.headers["Content-Encoding"] = "gzip"
        # replace gzip magic number with garbage
        invalid_encoded_content = gzip.compress(b"Hello world!").replace(
            b"\x1f\x8b", b"\xff\xff"
        )
        f.response.raw_content = invalid_encoded_content

        r = self.fetch("/flows/42/response/content.data")
        assert r.body == invalid_encoded_content
        assert r.code == 200

        f.revert()

    def test_update_flow_content(self):
        assert (
            self.fetch("/flows/42/request/content.data", method="POST", body="new").code
            == 200
        )
        f = self.view.get_by_id("42")
        assert f.request.content == b"new"
        assert f.modified()
        f.revert()

    def test_update_flow_content_multipart(self):
        body = (
            b"--somefancyboundary\r\n"
            b'Content-Disposition: form-data; name="a"; filename="a.txt"\r\n'
            b"\r\n"
            b"such multipart. very wow.\r\n"
            b"--somefancyboundary--\r\n"
        )
        assert (
            self.fetch(
                "/flows/42/request/content.data",
                method="POST",
                headers={
                    "Content-Type": 'multipart/form-data; boundary="somefancyboundary"'
                },
                body=body,
            ).code
            == 200
        )
        f = self.view.get_by_id("42")
        assert f.request.content == b"such multipart. very wow."
        assert f.modified()
        f.revert()

    def test_flow_contentview(self):
        assert get_json(self.fetch("/flows/42/request/content/raw")) == {
            "lines": [[["text", "foo"]], [["text", "bar"]]],
            "description": "Raw",
        }
        assert get_json(self.fetch("/flows/42/request/content/raw?lines=1")) == {
            "lines": [[["text", "foo"]]],
            "description": "Raw",
        }
        assert self.fetch("/flows/42/messages/content/raw").code == 400

    def test_flow_contentview_websocket(self):
        assert get_json(self.fetch("/flows/43/messages/content/raw?lines=2")) == [
            {
                "description": "Raw",
                "from_client": True,
                "lines": [[["text", "hello binary"]]],
                "timestamp": 946681203,
            },
            {
                "description": "Raw",
                "from_client": True,
                "lines": [[["text", "hello text"]]],
                "timestamp": 946681204,
            },
        ]

    def test_commands(self):
        resp = self.fetch("/commands")
        assert resp.code == 200
        assert get_json(resp)["set"]["help"]

    def test_command_execute(self):
        resp = self.fetch("/commands/unknown", method="POST")
        assert resp.code == 200
        assert get_json(resp) == {"error": "Unknown command: unknown"}
        resp = self.fetch("/commands/commands.history.get", method="POST")
        assert resp.code == 200
        assert get_json(resp) == {"value": []}

    def test_events(self):
        resp = self.fetch("/events")
        assert resp.code == 200
        assert get_json(resp)[0]["level"] == "info"

    def test_options(self):
        j = get_json(self.fetch("/options"))
        assert isinstance(j, dict)
        assert isinstance(j["anticache"], dict)

    def test_option_update(self):
        assert self.put_json("/options", {"anticache": True}).code == 200
        assert self.put_json("/options", {"wtf": True}).code == 400
        assert self.put_json("/options", {"anticache": "foo"}).code == 400

    def test_option_save(self):
        assert self.fetch("/options/save", method="POST").code == 200

    def test_err(self):
        with mock.patch("mitmproxy.tools.web.app.IndexHandler.get") as f:
            f.side_effect = RuntimeError
            assert self.fetch("/").code == 500

    @tornado.testing.gen_test
    def test_websocket(self):
        ws_url = f"ws://localhost:{self.get_http_port()}/updates"

        ws_client = yield websocket.websocket_connect(ws_url)
        self.master.options.anticomp = True

        r1 = yield ws_client.read_message()
        response = json.loads(r1)
        assert response == {
            "resource": "options",
            "cmd": "update",
            "data": {
                "anticomp": {
                    "value": True,
                    "choices": None,
                    "default": False,
                    "help": "Try to convince servers to send us un-compressed data.",
                    "type": "bool",
                }
            },
        }
        ws_client.close()

        # trigger on_close by opening a second connection.
        ws_client2 = yield websocket.websocket_connect(ws_url)
        ws_client2.close()

    def test_process_list(self):
        try:
            mitmproxy_rs.active_executables()
        except NotImplementedError:
            pytest.skip(
                "mitmproxy_rs.active_executables not available on this platform."
            )
        resp = self.fetch("/processes")
        assert resp.code == 200
        assert get_json(resp)

    def test_process_icon(self):
        try:
            mitmproxy_rs.executable_icon("invalid")
        except NotImplementedError:
            pytest.skip("mitmproxy_rs.executable_icon not available on this platform.")
        except Exception:
            pass
        resp = self.fetch("/executable-icon")
        assert resp.code == 400
        assert "Missing 'path' parameter." in resp.body.decode()

        resp = self.fetch("/executable-icon?path=invalid_path")
        assert resp.code == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.body == app.TRANSPARENT_PNG
