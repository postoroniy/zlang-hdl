// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');

// Keep the button held while checking progress: a redraw on release is too late.
async function checkContinuousPan({page,control,requests,setRenderDelay,root}){
  const canvas=page.locator('.viewport canvas').first();
  const box=await canvas.boundingBox(),overview=await page.locator('#overview').boundingBox();
  const range=async()=>(await control({command:'export_session'})).session.viewports[0];
  const viewportRequests=()=>requests.filter(request=>request.type==='renderRange'&&!request.overview);
  const clockInk=()=>canvas.evaluate(canvas=>{
    const context=canvas.getContext('2d'),scale=canvas.width/canvas.clientWidth;
    return [16,canvas.clientWidth-38].map(x=>{
      const pixels=context.getImageData(Math.round(x*scale),Math.round(38*scale),
        Math.round(22*scale),Math.round(22*scale)).data;
      let wave=0;
      for(let i=0;i<pixels.length;i+=4)if(pixels[i+1]>140&&pixels[i+1]>pixels[i]*1.3&&pixels[i+1]>pixels[i+2]*1.3)wave++;
      return wave;
    });
  });
  async function prepare(){
    setRenderDelay(0);
    await control({command:'set_viewport_range',start:36,end:60});
    await page.waitForTimeout(300);
    await page.evaluate(()=>{window.panRows=[...document.querySelectorAll('.displayRow')];});
    setRenderDelay(90);
    return viewportRequests().filter(request=>request.completed).length;
  }
  async function progress(before,label){
    assert.ok(viewportRequests().filter(request=>request.completed).length>before,
      label+' must fetch and display data during continuous movement, before release');
    assert.ok(viewportRequests().filter(request=>!request.completed).length<=1,
      label+' must not accumulate in-flight viewport queries');
    const coverage=await clockInk();
    assert.ok(coverage.every(pixels=>pixels>10),label+' must keep both viewport edges populated: '+coverage);
    assert.equal(await page.evaluate(()=>window.panRows.every((row,index)=>
      row===document.querySelectorAll('.displayRow')[index])),true,
    label+' must preserve row DOM and focus while time-only queries update values');
  }
  for(const button of ['middle','left'])for(const direction of [-1,1]){
    const before=await prepare(),initial=await range();
    await page.mouse.move(box.x+box.width/2,box.y+85);await page.mouse.down({button});
    for(let step=1;step<=36;step++){
      await page.mouse.move(box.x+box.width/2+direction*step*5,box.y+85);
      await page.waitForTimeout(12);
    }
    assert.notEqual((await range()).start,initial.start,button+' drag must move time before release');
    await progress(before,button+' pan '+direction);
    await page.screenshot({path:path.join(root,'pan-'+button+'-'+direction+'.png')});
    await page.mouse.up({button});
  }
  for(const direction of [-1,1]){
    const before=await prepare(),initial=await range();
    const x=overview.x+overview.width*(48/128);
    await page.mouse.move(x,overview.y+overview.height/2);await page.mouse.down();
    for(let step=1;step<=36;step++){
      await page.mouse.move(x+direction*step*3,overview.y+overview.height/2);
      await page.waitForTimeout(12);
    }
    assert.notEqual((await range()).start,initial.start);
    await progress(before,'overview pan '+direction);await page.mouse.up();
  }
  const before=await prepare(),initial=await range();
  await page.mouse.move(box.x+box.width/2,box.y+85);await page.keyboard.down('Shift');
  for(let step=0;step<36;step++){await page.mouse.wheel(0,8);await page.waitForTimeout(12);}
  assert.ok((await range()).start>initial.start);
  await progress(before,'wheel pan');await page.keyboard.up('Shift');
  await prepare();setRenderDelay(250);
  await control({command:'set_viewport_range',start:100,end:124});
  assert.deepEqual(await clockInk(),[0,0],
    'A tile outside the new viewport must not extrapolate a false constant clock');
  await page.waitForTimeout(650);
  assert.ok((await clockInk()).every(pixels=>pixels>10),'The requested range must replace the loading gap');
  setRenderDelay(0);
  await page.waitForTimeout(300);
}
async function checkDensePan({page,control,requests,root}){
  await page.locator('#addViewport').click();await page.locator('#syncViewports').check();
  await control({command:'set_viewport_range',start:300000,end:500000});
  await page.waitForTimeout(500);
  const canvas=page.locator('.viewport canvas').first(),box=await canvas.boundingBox();
  for(const direction of [-1,1]){
    const baseline=requests.length;
    await page.mouse.move(box.x+box.width/2,box.y+85);await page.mouse.down({button:'middle'});
    for(let step=1;step<=40;step++){
      await page.mouse.move(box.x+box.width/2+direction*step*3,box.y+85);
      await page.waitForTimeout(16);
    }
    const session=(await control({command:'export_session'})).session;
    assert.deepEqual(session.viewports[0],session.viewports[1]);
    for(let viewport=0;viewport<2;viewport++)assert.ok(requests.slice(baseline).some(request=>
      request.type==='renderRange'&&!request.overview&&request.viewport===viewport&&request.completed),
    'Dense synchronized viewport '+viewport+' must redraw before release');
    assert.ok(requests.filter(request=>request.type==='renderRange'&&!request.overview&&!request.completed).length<=1);
    await page.screenshot({path:path.join(root,'pan-dense-'+direction+'.png')});
    await page.mouse.up({button:'middle'});
  }
  await page.locator('#removeViewport').click();await page.locator('#fit').click();
}
module.exports={checkContinuousPan,checkDensePan};
