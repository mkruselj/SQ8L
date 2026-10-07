from gui_driver import Editor, find_command
import json
ed = Editor(program=256 + 45)   # MOOGLEAD
ed.click("buttFilt"); ed.screenshot("out/drv_filter.png")
ed.drag("lcdKnob0", dy=-30); ed.screenshot("out/drv_filter_drag.png")
tree = ed.popup_of(lambda: ed.click("menuFileImage"))
print("FILE menu:", [ (i["text"], i["id"]) for i in (tree or [])][:20])
tree = ed.popup_of(lambda: ed.click("menuOptImage"))
print("OPTIONS menu:", json.dumps(tree)[:600])
