"""新增输入模型不能阻止已有 CANoe 事件资源清理。"""

from types import SimpleNamespace

from anda.dynamic.pycanoe import _event_sink_disconnect_targets


def test_canoe_event_scan_skips_unrelated_objects_with_guarded_dictionary():
    class Guarded:
        @property
        def __dict__(self):
            raise RuntimeError("synthetic library object cannot expose attributes")

    unrelated = Guarded()
    event = SimpleNamespace(_olecp=object(), _olecp_cookie=17)
    root = SimpleNamespace(event=event, unrelated=unrelated)
    # 无关对象留在 GC 集合中，正常 CANoe 事件仍应被保留供后续清理。
    targets = _event_sink_disconnect_targets(
        SimpleNamespace(event=event), include_gc=True
    )
    assert root.unrelated is unrelated
    assert event in targets
