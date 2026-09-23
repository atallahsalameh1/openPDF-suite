"""M4 live smoke: Add Text + paragraph reflow against the real worker."""
import multiprocessing
import sys
import time


def main() -> int:
    from openpdfsuite.app import create_app, open_window
    from openpdfsuite.domain.models import EditMode

    app = create_app(sys.argv)
    w = open_window()
    w.open_document(r"tests\fixtures\generated\standard.pdf")
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if w.controller and w.controller.session:
            break
    assert w.controller and w.controller.session
    while not (w.view and w.view.frames and w.view.frames[0].pixmap) \
            and time.time() < deadline:
        app.processEvents()
    print("OPENED", w.controller.session.meta.page_count, "pages", flush=True)

    # -- Add Text
    w.action_tool_add.trigger()
    w.view.add_text_requested.emit(0, 90.0, 320.0)
    assert w._overlay is not None
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("Smoke added line.")
    t0 = time.time()
    w._overlay.apply()
    while w.controller.session.revision != 1 and time.time() < deadline:
        app.processEvents()
    print("ADD-TEXT committed rev", w.controller.session.revision,
          f"in {time.time() - t0:.1f}s", flush=True)

    # -- paragraph reflow with resized box
    w.action_tool_edit.trigger()
    while not w.view.page_regions.get(0) and time.time() < deadline:
        app.processEvents()
    para = next(r for r in w.view.page_regions[0]
                if r.mode == EditMode.REFLOW_BOX)
    w._on_region_clicked(0, para)
    assert w.view.layout_box_rect is not None
    from openpdfsuite.ui.document_view import PAGE_MARGIN

    frame = w.view.frames[0]
    orig = w.view.layout_box_rect[1]
    mid_x = (orig[0] + orig[2]) / 2
    from PySide6.QtCore import QPoint

    start = QPoint(int(PAGE_MARGIN + mid_x * frame.zoom),
                   int(PAGE_MARGIN + orig[3] * frame.zoom))
    w.view._box_drag = {"op": "resize", "handle": "b",
                        "start": frame.map_to_page(start), "orig": orig, "page": 0}
    w.view._update_box_drag(
        frame, QPoint(int(PAGE_MARGIN + mid_x * frame.zoom),
                      int(PAGE_MARGIN + 228.0 * frame.zoom)))
    w.view._frame_mouse_release(frame, None)
    print("BOX resized to", [round(v, 1) for v in w._layout_box[1]], flush=True)
    w._on_region_double_clicked(0, para)
    w._overlay.editor.setPlainText("word " * 44)
    w._overlay.apply()
    while w.controller.session.revision != 2 and time.time() < deadline:
        app.processEvents()
    print("REFLOW committed rev", w.controller.session.revision, flush=True)

    # undo both, save
    w._on_undo()
    while w.controller.session.revision != 1 and time.time() < deadline:
        app.processEvents()
    w._on_undo()
    while w.controller.session.revision != 0 and time.time() < deadline:
        app.processEvents()
    print("UNDO back to rev", w.controller.session.revision, flush=True)
    end = time.time() + 0.3
    while time.time() < end:
        app.processEvents()
    w.close()
    print("SMOKE-OK", flush=True)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
