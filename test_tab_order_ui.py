"""Execute the tab reorder transaction against a tiny deterministic DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=js.slice(js.indexOf('function _visibleTabOrder()'),js.indexOf('function renderNav()',js.indexOf('function _visibleTabOrder()')));

class Classes {
  constructor(values=[]){this.values=new Set(values)}
  add(value){this.values.add(value)}
  remove(value){this.values.delete(value)}
  contains(value){return this.values.has(value)}
}
class Item {
  constructor(name,kind='item'){
    this.kind=kind;
    this.dataset=name?{session:name}:{};
    this.classList=new Classes(kind==='item'?['nav-item']:['nav-'+kind]);
  }
  getBoundingClientRect(){
    const index=nav.children.indexOf(this);
    return {left:index*100,width:100,right:(index+1)*100};
  }
  addEventListener(){}
}
const alpha=new Item('alpha'),bravo=new Item('bravo'),charlie=new Item('charlie');
const add=new Item('', 'new-btn'),spacer=new Item('', 'spacer');
const nav={
  children:[alpha,bravo,charlie,add,spacer],scrollLeft:0,
  querySelectorAll(selector){
    let rows=this.children.filter(item=>item.kind==='item');
    if(selector.includes(':not(.dragging)'))rows=rows.filter(item=>!item.classList.contains('dragging'));
    if(selector.includes('.dragging'))rows=rows.filter(item=>item.classList.contains('dragging'));
    return rows;
  },
  querySelector(selector){
    if(selector==='.nav-item.dragging')return this.children.find(item=>item.kind==='item'&&item.classList.contains('dragging'))||null;
    if(selector==='.nav-new-btn')return add;
    if(selector==='.nav-spacer')return spacer;
    return null;
  },
  insertBefore(item,before){
    this.children=this.children.filter(row=>row!==item);
    const index=before?this.children.indexOf(before):-1;
    if(index<0)this.children.push(item);else this.children.splice(index,0,item);
  },
  getBoundingClientRect(){return {left:0,right:500}},
};
let request=null,alerts=[];
const context=vm.createContext({
  console,Promise,Math,Map,Array,BASE:'',navEl:nav,
  sessions:[{name:'alpha'},{name:'bravo'},{name:'charlie'}],
  _tabOrderDragging:false,_tabOrderSuppressClick:false,_tabOrderSaveSequence:0,_tabTouchCandidate:null,
  document:{body:{classList:new Classes()}},
  setTimeout:fn=>fn(),clearTimeout(){},
  fetch:async(url,options)=>{request={url,options};return {ok:true}},
  reconcileSessionRoster:async()=>{},alert:value=>alerts.push(value),
});
vm.runInContext(region,context);
context._startTabOrderDrag(alpha);
context._moveDraggedTab(999);
context._finishTabOrderDrag();
setImmediate(()=>process.stdout.write(JSON.stringify({
  dom:context._visibleTabOrder(),
  rows:context.sessions.map(row=>row.name),
  url:request&&request.url,
  headers:request&&request.options.headers,
  body:request&&JSON.parse(request.options.body),
  alerts,
})));
"""


def test_drag_reorders_rows_and_persists_the_complete_visible_order():
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["dom"] == ["bravo", "charlie", "alpha"]
    assert state["rows"] == state["dom"]
    assert state["url"] == "/api/session-tab-order"
    assert state["headers"]["X-Tmux-Tab-Order"] == "1"
    assert state["body"] == {"sessions": state["dom"]}
    assert state["alerts"] == []
