/**
 * Google Apps Script receiver for the swejobs bot.
 *
 * Setup (one time, ~3 min):
 *  1. Create a Google Sheet, then Extensions > Apps Script.
 *  2. Paste this file, replace SECRET below with a long random string.
 *  3. Deploy > New deployment > Web app; execute as "Me",
 *     access "Anyone". Copy the /exec URL.
 *  4. On the server: echo '<exec-url>?secret=<SECRET>' > ~/swejobs/sheet_webhook
 *
 * The bot POSTs {rows:[{key, first_seen, company, position, location, salary,
 * sources, url, status, removed_at}]}. Rows are upserted by `key` (column A).
 * Columns K+ (e.g. "applied", "notes") are yours; the bot never writes them.
 */
var SECRET = 'CHANGE_ME';
var SHEET_NAME = 'Postings';
var HEADER = ['key', 'first_seen', 'company', 'position', 'location',
              'salary', 'sources', 'url', 'status', 'removed_at'];

function doPost(e) {
  if (!e || !e.parameter || e.parameter.secret !== SECRET) {
    return json_({ ok: false, error: 'bad secret' });
  }
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var ss = SpreadsheetApp.getActive();
    var sh = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
    if (sh.getLastRow() === 0) {
      sh.appendRow(HEADER.concat(['applied', 'notes']));
      sh.setFrozenRows(1);
    }
    var rows = JSON.parse(e.postData.contents).rows || [];
    var last = sh.getLastRow();
    var index = {}; // key -> row number
    if (last > 1) {
      var keys = sh.getRange(2, 1, last - 1, 1).getValues();
      for (var i = 0; i < keys.length; i++) index[keys[i][0]] = i + 2;
    }
    var appended = 0, updated = 0, toAppend = [];
    rows.forEach(function (r) {
      var vals = HEADER.map(function (h) { return r[h] || ''; });
      var at = index[r.key];
      if (at) {
        sh.getRange(at, 1, 1, HEADER.length).setValues([vals]);
        updated++;
      } else {
        toAppend.push(vals);
        index[r.key] = last + toAppend.length; // handle dup keys in one batch
        appended++;
      }
    });
    if (toAppend.length) {
      sh.getRange(last + 1, 1, toAppend.length, HEADER.length)
        .setValues(toAppend);
    }
    return json_({ ok: true, appended: appended, updated: updated });
  } finally {
    lock.releaseLock();
  }
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}
