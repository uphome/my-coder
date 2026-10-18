"""多会话分屏画布 `/split`（issue #54 ④ 的最小落地）。

判据：
① `/split` 能打开，页面里用**独立的 iframe** 载完整界面（多画布靠"每格一个独立页面上下文"，
   不是把现有界面重写成多面板）；
② 现有单页 `/` 不受影响（回归）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from my_coder import web


def _client(tmp_path):
    web.init_web(tmp_path, fake=True, sessions_dir=tmp_path / 'sess')
    return TestClient(web.app)


def test_split_page_serves_independent_frames(tmp_path):
    client = _client(tmp_path)
    response = client.get('/split')
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="panes"' in body, '缺少放格子的容器'
    assert "frame.src = '/'" in body, '格子必须载完整界面（/）'
    assert 'data-panes="3"' in body, '应提供三格开关'


def test_index_still_serves_the_single_page(tmp_path):
    client = _client(tmp_path)
    response = client.get('/')
    assert response.status_code == 200
    assert '<title>MyCoder</title>' in response.text
