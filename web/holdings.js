/* 持仓编辑器 + 调仓结果渲染（不依赖框架，和 index.html 共用全局 table/fmt/esc） */
(function () {
  var $ = function (s) { return document.querySelector(s); };

  function assetOptions(sel) {
    var opts = [['etf', 'ETF'], ['cb', '可转债'], ['stock', '个股'], ['dividend', '红利股'], ['growth', '成长股']];
    return opts.map(function (p) {
      return '<option value="' + p[0] + '"' + (p[0] === sel ? ' selected' : '') + '>' + p[1] + '</option>';
    }).join('');
  }

  function rowHtml(r) {
    r = r || {};
    return '<tr>'
      + '<td><input class="h-code" value="' + esc(r.code || '') + '" placeholder="sh512800 / 113046" style="width:150px"></td>'
      + '<td><input class="h-name" value="' + esc(r.name || '') + '" placeholder="名称" style="width:130px"></td>'
      + '<td><input class="h-qty" type="number" step="1" min="0" value="' + (r.quantity != null ? r.quantity : '') + '" style="width:90px"></td>'
      + '<td><input class="h-cost" type="number" step="0.001" min="0" value="' + (r.cost_price != null ? r.cost_price : '') + '" style="width:90px"></td>'
      + '<td><select class="h-asset">' + assetOptions(r.asset || 'etf') + '</select></td>'
      + '<td><button class="ghost h-del" title="删除">删除</button></td>'
      + '</tr>';
  }

  function renderEditor(rows) {
    var t = $('#tblEditHoldings');
    if (!t) return;
    var body = rows.length ? rows.map(rowHtml).join('')
      : '<tr><td colspan="6" class="mut">还没有持仓，点「+ 添加一行」，或用命令行 holdings import</td></tr>';
    t.innerHTML = '<thead><tr><th>代码</th><th>名称</th><th>数量</th><th>成本价</th><th>类别</th><th></th></tr></thead>'
      + '<tbody>' + body + '</tbody>';
  }

  function renderSummary(h) {
    var n = (h.positions || []).length;
    $('#holdingsHint').textContent = n ? (n + ' 只持仓') : '未录入';
    $('#holdingsSummary').innerHTML = n
      ? '<span>总资产 <b>' + fmt(h.equity, 0) + '</b></span>'
        + '<span>持仓市值 <b>' + fmt(h.market_value, 0) + '</b></span>'
        + '<span>现金 <b>' + fmt(h.cash, 0) + '</b></span>'
        + '<span>浮盈 <b>' + fmt(h.unrealized_pnl, 0) + '</b></span>'
        + '<span>仓位 <b>' + fmt(h.invested_pct, 1) + '%</b></span>'
      : '<span class="mut">录入持仓后，分析会自动切到「调仓」模式，不用每次重填。</span>';
  }

  async function loadHoldings(live) {
    var h = await api('/api/holdings' + (live ? '?live=1' : ''));
    state.holdings = h;
    $('#cashInput').value = Math.round(h.cash || 0);
    renderEditor(h.positions || []);
    renderSummary(h);
  }

  async function saveHoldings() {
    var rows = [];
    var trs = $('#tblEditHoldings').querySelectorAll('tbody tr');
    for (var i = 0; i < trs.length; i++) {
      var tr = trs[i];
      var codeEl = tr.querySelector('.h-code');
      if (!codeEl) continue;
      var qty = Number(tr.querySelector('.h-qty').value) || 0;
      var code = codeEl.value.trim();
      if (!code || qty <= 0) continue;
      rows.push({
        code: code,
        name: tr.querySelector('.h-name').value.trim(),
        quantity: qty,
        cost_price: Number(tr.querySelector('.h-cost').value) || 0,
        asset: tr.querySelector('.h-asset').value
      });
    }
    var cash = Number($('#cashInput').value) || 0;
    var r = await api('/api/holdings', { cash: cash, rows: rows });
    if (!r.ok) { alert(r.error || '保存失败'); return; }
    state.holdings = r;
    $('#holdingsHint').textContent = '已保存 ' + rows.length + ' 只';
    renderSummary(r);
    renderEditor(r.positions || []);
  }

  $('#btnSaveHoldings').onclick = saveHoldings;
  $('#btnRefreshHoldings').onclick = function () { loadHoldings(true); };
  $('#btnAddRow').onclick = function () {
    var tb = $('#tblEditHoldings tbody');
    if (tb.querySelector('td[colspan]')) tb.innerHTML = '';
    tb.insertAdjacentHTML('beforeend', rowHtml({ asset: 'etf' }));
  };
  $('#tblEditHoldings').addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('h-del')) {
      e.target.closest('tr').remove();
    }
  });

  function renderExtra(res) {
    var isReb = res.mode === 'rebalance' && (res.positions || []).length > 0;
    $('#cardPositions').classList.toggle('hide', !isReb);
    $('#cardRebalance').classList.toggle('hide', !isReb);
    if (!isReb) {
      $('#cardBuy').querySelector('h2').textContent = '5 · 目标组合（新建仓）';
      return;
    }
    $('#cardBuy').querySelector('h2').textContent = '5 · 目标组合（调仓后）';

    var a = res.account || {};
    $('#overview').innerHTML = '<span>档位 <b>' + esc(res.tier_name) + '</b></span>'
      + '<span>总资产 <b>' + fmt(a.equity || res.equity, 0) + '</b></span>'
      + '<span>持仓市值 <b>' + fmt(a.market_value, 0) + '</b></span>'
      + '<span>现金 <b>' + fmt(a.cash, 0) + '</b></span>'
      + '<span>仓位 <b>' + fmt(a.invested_pct, 1) + '%</b></span>'
      + '<span>浮盈 <b>' + fmt(a.unrealized_pnl, 0) + '</b></span>';

    table('#tblPositions', [
      { k: '代码', t: '代码' }, { k: '名称', t: '名称' }, { k: '类别', t: '类别' },
      { k: '数量', t: '数量', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '成本价', t: '成本价', num: 1, f: function (v) { return fmt(v, 3); } },
      { k: '现价', t: '现价', num: 1, f: function (v) { return fmt(v, 3); } },
      { k: '市值', t: '市值', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '浮盈', t: '浮盈', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '浮盈%', t: '浮盈%', num: 1, f: function (v) { return fmt(v, 1); } },
      { k: '权重%', t: '权重', num: 1, f: function (v) { return fmt(v, 1); } }
    ], res.positions || []);

    table('#tblRebalance', [
      { k: '动作', t: '动作' }, { k: '类别', t: '类别' }, { k: '代码', t: '代码' }, { k: '名称', t: '名称' },
      { k: '当前数量', t: '当前', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '目标数量', t: '目标', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '差额', t: '差额', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '委托价', t: '委托价', num: 1, f: function (v) { return fmt(v, 3); } },
      { k: '金额', t: '金额', num: 1, f: function (v) { return fmt(v, 0); } },
      { k: '理由', t: '理由' }
    ], res.rebalance || []);

    var wb = $('#rebalanceWarn');
    if ((res.warnings || []).length) {
      wb.classList.remove('hide');
      wb.textContent = '⚠️ ' + res.warnings.join('；');
    } else {
      wb.classList.add('hide');
    }
  }

  var orig = window.renderResult;
  window.renderResult = function (res) {
    orig(res);
    try { renderExtra(res); } catch (e) { console.error('renderExtra', e); }
  };

  loadHoldings(false);
})();
