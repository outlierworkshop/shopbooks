/* ShopBooks folder picker: a lightweight server-backed directory browser for the plain-text folder
 * path fields on Settings (statement/receipt watchers, the extra backup folder). Browsers
 * deliberately never expose a real filesystem path from <input type="file">/webkitdirectory (privacy
 * sandboxing) - but ShopBooks itself is a local server with full filesystem access, so this asks
 * the server to list directories instead of trying to fight the browser sandbox.
 * No-op unless the picker markup (#folderPickerModal) is present on the page.
 */
(function () {
  'use strict';
  if (!document.getElementById('folderPickerModal')) return;

  var targetInput = null;
  var currentParent = null;

  function $(id) { return document.getElementById(id); }

  function render(data) {
    $('folderPickerPath').value = data.path;
    currentParent = data.parent;
    var list = $('folderPickerList');
    list.innerHTML = '';
    if (!data.dirs.length) {
      var empty = document.createElement('div');
      empty.className = 'folder-picker-empty muted';
      empty.textContent = 'No subfolders here.';
      list.appendChild(empty);
    }
    data.dirs.forEach(function (d) {
      var row = document.createElement('div');
      row.className = 'folder-picker-row';
      row.textContent = '📁 ' + d.name;   // 📁
      row.addEventListener('click', function () { go(d.path); });
      list.appendChild(row);
    });
  }

  function go(path) {
    fetch('/settings/browse-folder?path=' + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }

  window.openFolderPicker = function (inputId) {
    targetInput = $(inputId);
    $('folderPickerOverlay').style.display = 'block';
    $('folderPickerModal').style.display = 'flex';
    go(targetInput.value || '');
  };

  window.closeFolderPicker = function () {
    $('folderPickerOverlay').style.display = 'none';
    $('folderPickerModal').style.display = 'none';
    targetInput = null;
  };

  window.folderPickerGo = function (path) { go(path); };
  window.folderPickerUp = function () { if (currentParent) go(currentParent); };

  window.chooseFolderPickerPath = function () {
    if (targetInput) targetInput.value = $('folderPickerPath').value;
    closeFolderPicker();
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && $('folderPickerModal').style.display !== 'none') closeFolderPicker();
  });
})();
