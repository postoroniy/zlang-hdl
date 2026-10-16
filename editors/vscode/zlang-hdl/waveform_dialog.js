// SPDX-License-Identifier: Apache-2.0
// Text editing uses a webview dialog; native prompt() is unavailable in VS Code.
function promptText(label,value='') {
  const dialog=el('textDialog');
  if(dialog.open)return Promise.resolve(null);
  el('textDialogLabel').textContent=label;el('textDialogValue').value=value;
  return new Promise(resolve=>{
    dialog.returnValue='cancel';
    dialog.addEventListener('close',()=>resolve(dialog.returnValue==='ok'?el('textDialogValue').value:null),{once:true});
    dialog.showModal();el('textDialogValue').focus();el('textDialogValue').select();
  });
}
module.exports={promptText};
