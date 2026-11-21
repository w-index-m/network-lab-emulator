const pptxgen = require('pptxgenjs');
const { applyTheme } = require('/root/.claude/skills/synced/873d3bbd-f5ee-4b8c-9a98-2572553cb7c0_eea210e2-e365-400d-9ef2-489ddbf84912/pptx/scripts/apply_theme.js');

const OUT = process.argv[2] || 'deck.pptx';
const THEME = {
  name: 'Nirvana Behind',
  headFontFace: 'Meiryo',
  bodyFontFace: 'Meiryo',
  colors: {
    dk1: '070D17', lt1: 'FFFFFF', dk2: '13213A', lt2: 'DBE6F3',
    accent1: '4FD6E8', accent2: 'F2A93B', accent3: 'FF5A6A', accent4: 'A98BFF',
    accent5: '5FE0A0', accent6: '8BA0B8', hlink: '4FD6E8', folHlink: 'A98BFF',
  },
};

const pres = new pptxgen();
pres.layout = 'LAYOUT_16x9'; // 10 x 5.625
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = 'AI ドリブン開発の裏側';
pres.author = 'Claude Code';
const C = pres.SchemeColor;
const SH = pres.shapes;

// ── layouts ──
pres.defineSlideMaster({
  title: 'TITLE_DARK',
  background: { color: C.text1 },
  objects: [
    { placeholder: { options: { name: 'title', type: 'title', x: 0.6, y: 1.55, w: 8.8, h: 1.4, fontSize: 36, bold: true, color: C.background1, valign: 'top', align: 'left', margin: 0 }, text: '' } },
    { placeholder: { options: { name: 'body', type: 'body', x: 0.6, y: 3.05, w: 8.8, h: 1.5, fontSize: 16, color: C.accent1, valign: 'top', align: 'left', margin: 0 }, text: '' } },
  ],
});
pres.defineSlideMaster({
  title: 'CONTENT_DARK',
  background: { color: C.text1 },
  margin: [0.5, 0.5, 0.5, 0.5],
  objects: [
    { placeholder: { options: { name: 'title', type: 'title', x: 0.5, y: 0.3, w: 9.0, h: 0.7, fontSize: 26, bold: true, color: C.background1, valign: 'middle', align: 'left', margin: 0 }, text: '' } },
    { text: { text: 'Nirvana Lab ・ AI ドリブン開発の裏側', options: { x: 0.5, y: 5.2, w: 6, h: 0.3, fontSize: 9, color: C.accent6, margin: 0 } } },
  ],
  slideNumber: { x: 9.0, y: 5.2, w: 0.5, h: 0.3, fontSize: 9, color: C.accent6, align: 'right' },
});

// ── helpers ──
function hex(slide, x, y, size, color, label, name, labelColor) {
  slide.addShape(SH.HEXAGON, { x, y, w: size, h: size * 0.88, fill: { color }, line: { color, width: 0 }, objectName: name });
  if (label) {
    slide.addText(label, { x, y, w: size, h: size * 0.88, align: 'center', valign: 'middle', fontSize: Math.round(size * 18), bold: true, color: labelColor || C.text1, margin: 0, isTextBox: true, objectName: name + ' label' });
  }
}
function card(slide, x, y, w, h, name) {
  slide.addShape(SH.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: C.text2, width: 0 }, objectName: name });
}
function txt(slide, text, opt) {
  slide.addText(text, Object.assign({ isTextBox: true, margin: 0, color: C.background2, fontSize: 14, valign: 'top' }, opt));
}

// 1. タイトル
pres.addSection({ title: 'はじめに' });
let s = pres.addSlide({ masterName: 'TITLE_DARK', sectionTitle: 'はじめに' });
s.addText('AI ドリブン開発の裏側', { placeholder: 'title' });
s.addText('Claude がニルヴァーナ ラボで、依頼から main への取り込みまで何をしているか', { placeholder: 'body' });
hex(s, 7.75, 0.45, 0.9, C.accent1, '情', 'motif hex info');
hex(s, 8.55, 0.95, 0.65, C.accent2, '注', 'motif hex warn');
hex(s, 7.95, 1.25, 0.5, C.accent3, '警', 'motif hex alert', C.background1);
txt(s, '2026-11-20', { x: 0.6, y: 4.7, w: 4, h: 0.3, fontSize: 12, color: C.accent6 });
s.addNotes('このデッキは、ニルヴァーナ ラボの開発で Claude が裏側で何をしているかをまとめたものです。依頼は一言、そこから調査・実装・テスト・本物のツールでの確認・GitHub の CI・main への取り込みまでを Claude が進めます。');

// 2. 要旨
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'はじめに' });
s.addText('一言の依頼が、確かめられた変更として main に入る', { placeholder: 'title' });
const pillars = [
  ['任せる', C.accent1, '人は「何をしたいか」を一言で。Claude がコードを読み、作り、テストを書く'],
  ['確かめる', C.accent2, '単体テストに加えて、本物のサーバーと本物のツール(snmpwalk・gnmic・ブラウザ)で動きを見る'],
  ['直し切る', C.accent3, 'テストが落ちたら再実行で済ませず、原因を再現してから直す'],
];
pillars.forEach(([head, col, body], i) => {
  const x = 0.5 + i * 3.05;
  card(s, x, 1.3, 2.85, 3.5, 'pillar card ' + (i + 1));
  hex(s, x + 0.3, 1.6, 0.75, col, String(i + 1), 'pillar hex ' + (i + 1));
  txt(s, head, { x: x + 0.3, y: 2.45, w: 2.3, h: 0.5, fontSize: 20, bold: true, color: C.background1 });
  txt(s, body, { x: x + 0.3, y: 3.0, w: 2.3, h: 1.65, fontSize: 13 });
});
s.addNotes('要点は三つです。任せる、確かめる、直し切る。今日の作業(SNMP のポートカウンタ、gNMI、STP の修正)もこの流れで進めました。');

// 3. 開発ループ
pres.addSection({ title: '進め方' });
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '進め方' });
s.addText('1 つの依頼が通る 8 つの工程', { placeholder: 'title' });
const steps = [
  ['依頼', '一言で方向を伝える'], ['読む', 'コードを調べる'], ['作る', '実装する'], ['単体テスト', 'サンドボックス内'],
  ['本物で確認', 'サンドボックス内の本物のツールで'], ['全体テスト', '約 2,100 件・17 分'], ['PR と CI', 'GitHub で同じテストをもう一度'], ['取り込み', '緑なら main へ'],
];
steps.forEach(([h, b], i) => {
  const row = Math.floor(i / 4), col = i % 4;
  const x = 0.5 + col * 2.3, y = 1.3 + row * 1.85;
  hex(s, x, y, 0.6, i === 4 ? C.accent2 : C.accent1, String(i + 1), 'step hex ' + (i + 1));
  txt(s, h, { x: x + 0.72, y: y + 0.02, w: 1.45, h: 0.35, fontSize: 15, bold: true, color: C.background1 });
  txt(s, b, { x: x + 0.72, y: y + 0.4, w: 1.45, h: 0.9, fontSize: 11, color: C.background2 });
});
txt(s, '4〜6 は LLM サンドボックス(次のスライド)の中で動かす。落ちたら「読む」に戻る', { x: 0.5, y: 4.85, w: 9, h: 0.3, fontSize: 12, italic: true, color: C.accent2 });
s.addNotes('工程は 8 つです。特徴は 5 番目の「本物で確認」です。単体テストが通っても、実際に起動したサーバーに本物の snmpwalk や gnmic を当てて、数字が期待どおり動くかを見ます。ここで見つかるバグが多いです。');

// 3b. LLM サンドボックス
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '進め方' });
s.addText('テストを動かす場所: LLM サンドボックス', { placeholder: 'title' });
// 左: 流れの図
const flow = [
  ['Claude', '読む・書く・判断する', C.accent1, 0.95],
  ['LLM サンドボックス', 'クラウド上の使い捨ての Linux コンテナ', C.accent2, 2.1],
  ['GitHub', 'push・PR・CI', C.accent4, 3.95],
];
flow.forEach(([h, b, col, y], i) => {
  const hh = i === 1 ? 1.65 : 0.8;
  s.addShape(SH.ROUNDED_RECTANGLE, { x: 0.5, y: y + 0.25, w: 4.4, h: hh, rectRadius: 0.08, fill: { color: C.text2 }, line: { color: col, width: 1.5 }, objectName: 'flow box ' + (i + 1) });
  txt(s, h, { x: 0.7, y: y + 0.32, w: 4.0, h: 0.35, fontSize: 15, bold: true, color: col });
  txt(s, b, { x: 0.7, y: y + 0.65, w: 4.0, h: 0.3, fontSize: 11, color: C.background2 });
});
const chips = ['エミュレータ', 'pytest', 'snmpwalk', 'gnmic', 'Playwright'];
chips.forEach((t, i) => {
  const cx = 0.7 + (i % 3) * 1.4, cy = 3.05 + Math.floor(i / 3) * 0.38;
  s.addShape(SH.ROUNDED_RECTANGLE, { x: cx, y: cy, w: 1.3, h: 0.3, rectRadius: 0.05, fill: { color: C.text1 }, line: { color: C.accent2, width: 0.75 }, objectName: 'chip ' + t });
  txt(s, t, { x: cx, y: cy, w: 1.3, h: 0.3, fontSize: 9, align: 'center', valign: 'middle', color: C.background1 });
});
// 右: 特徴
const feats = [
  ['隔離されている', 'あなたの PC や実機には触れない'],
  ['本物を動かせる', 'サーバーを起動し、snmpwalk などを当てる'],
  ['外への通信は許可制', 'PyTorch の配布元などは届かない'],
  ['使い捨て', '途中で入れ替わることも。残す物は Git へ'],
];
feats.forEach(([h, b], i) => {
  const y = 1.25 + i * 0.92;
  hex(s, 5.25, y, 0.45, C.accent2, String(i + 1), 'feat hex ' + (i + 1));
  txt(s, h, { x: 5.85, y: y - 0.02, w: 3.65, h: 0.32, fontSize: 14, bold: true, color: C.background1 });
  txt(s, b, { x: 5.85, y: y + 0.3, w: 3.65, h: 0.55, fontSize: 11 });
});
s.addNotes('4 から 6 の工程は、LLM サンドボックスの中で動いています。クラウド上に用意された使い捨ての Linux コンテナで、Claude はここでエミュレータのサーバーを起動し、net-snmp や gnmic、Playwright を入れて本物のツールを当てます。ユーザーの PC や実機からは隔離されていて、外への通信は許可されたものだけです。コンテナは使い捨てなので、残したい物は Git に push します。今日も途中でコンテナが入れ替わり、依存を入れ直しました。');

// 3c. サンドボックスの中身と、さばける量
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '進め方' });
s.addText('サンドボックスの中身と、さばける量', { placeholder: 'title' });
const SPEC = { bold: true, color: C.text1, fill: { color: C.accent2 }, fontSize: 12 };
const sp = (a, b) => [{ text: a, options: { bold: true, color: C.background1 } }, { text: b }];
s.addTable([
  [{ text: '項目', options: SPEC }, { text: '2026-11-20 時点', options: SPEC }],
  sp('OS', 'Ubuntu 24.04.5 LTS'),
  sp('CPU', '4 コア(Xeon 2.1 GHz)'),
  sp('メモリ', '15 GB'),
  sp('ディスク', '空き 27 GB(使い切ると書き込めない)'),
  sp('言語', 'Python 3.11 ・ Node 22 ・ Go 1.24'),
  sp('外への通信', '許可制のプロキシ経由'),
], { x: 0.5, y: 1.2, w: 4.4, colW: [1.2, 3.2], fontSize: 11, color: C.background2, fill: { color: C.text2 }, border: { type: 'solid', pt: 1, color: C.text1 }, rowH: 0.42, valign: 'middle', objectName: 'spec table' });
s.addChart(pres.charts.BAR, [{ name: '失敗率(%)', labels: ['20 同時', '50 同時', '200 同時'], values: [0, 0.09, 4.73] }], {
  x: 5.15, y: 1.15, w: 4.35, h: 2.55, barDir: 'col', objectName: 'k6 failure chart',
  showTitle: true, title: '同時アクセス数と失敗率(%)', titleFontSize: 11, titleColor: THEME.colors.lt1, titleFontFace: '+mn-lt',
  chartColors: [THEME.colors.accent3], showValue: true, dataLabelPosition: 'outEnd', dataLabelColor: THEME.colors.lt1, dataLabelFontSize: 10, dataLabelFontFace: '+mn-lt', dataLabelFormatCode: '0.00',
  catAxisLabelColor: THEME.colors.lt2, valAxisLabelColor: THEME.colors.lt2, catAxisLabelFontSize: 10, valAxisLabelFontSize: 9,
  catAxisLabelFontFace: '+mn-lt', valAxisLabelFontFace: '+mn-lt',
  valGridLine: { color: THEME.colors.dk2, size: 0.5 }, catGridLine: { style: 'none' }, showLegend: false,
});
card(s, 5.15, 3.85, 4.35, 1.15, 'cps card');
txt(s, '約 100/秒', { x: 5.35, y: 3.92, w: 1.75, h: 0.6, fontSize: 24, bold: true, color: C.accent2, valign: 'middle' });
txt(s, '新規接続(SSH)の上限。超えると失敗が急増', { x: 7.1, y: 3.95, w: 2.3, h: 0.6, fontSize: 11 });
txt(s, '壁はサンドボックスでなく、エミュレータの中継の作り', { x: 5.35, y: 4.55, w: 4.05, h: 0.4, fontSize: 10, color: C.accent6 });
s.addNotes('サンドボックスは Ubuntu 24.04.5 LTS、4 コア、メモリ 15 GB です。以前 k6 で ALB のエミュレータに負荷をかけたとき、同時 20 アクセスなら失敗 0%、50 で 0.09%、200 で 4.73% でした。SSH の新規接続は 1 秒あたり約 100 で頭打ちです。どちらも、壁はサンドボックスの性能ではなく、エミュレータの中継部分が 1 接続ごとにスレッドを 2 本使う作りであることが原因です。数百〜数千の同時接続を試すには、非同期 I/O への作り替えが次の一手になります。');

// 4. テストの 4 層
pres.addSection({ title: 'テスト' });
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('テストは 4 層。下ほど数が多く、上ほど本物に近い', { placeholder: 'title' });
const layers = [
  ['GitHub CI', 'push と PR のたびに自動', C.accent4, 4.4],
  ['本物のツールで確認', 'snmpwalk・gnmic・Playwright', C.accent2, 5.0],
  ['シナリオ回帰', '本物のサーバーで 12 シナリオ・62 チェック', C.accent5, 5.6],
  ['pytest', '172 ファイル・約 2,160 件', C.accent1, 6.2],
];
layers.forEach(([h, b, col, w], i) => {
  const y = 1.2 + i * 0.92;
  const x = 0.5 + (6.2 - w) / 2;
  s.addShape(SH.ROUNDED_RECTANGLE, { x, y, w, h: 0.8, rectRadius: 0.08, fill: { color: col }, line: { color: col, width: 0 }, objectName: 'layer ' + h });
  txt(s, [{ text: h, options: { bold: true, fontSize: 15, breakLine: true } }, { text: b, options: { fontSize: 11 } }],
      { x, y, w, h: 0.8, align: 'center', valign: 'middle', color: C.text1 });
});
txt(s, [
  { text: 'なぜ 4 層か', options: { bold: true, color: C.background1, fontSize: 15, breakLine: true } },
  { text: 'pytest は速く何度でも回せるが、作り物の中で完結する。', options: { breakLine: true } },
  { text: '本物のツールは、実機の監視ツールから見て正しいかを確かめる。', options: { breakLine: true } },
  { text: 'CI は「自分の環境では通る」を防ぐ最後の関門。', options: {} },
], { x: 7.0, y: 1.3, w: 2.5, h: 3.5, fontSize: 12, paraSpaceAfter: 8 });
s.addNotes('テストは 4 層です。一番下の pytest が約 2,160 件。その上のシナリオ回帰は、本物のサーバーを起動して OSPF・BGP・STP などを 12 シナリオで確かめます。さらに本物のツールでの確認、最後に GitHub の CI で同じテストをもう一度回します。');

// 4b. pytest と仲間の道具 / PyTorch の位置づけ
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('テストの道具と、テストされる側の PyTorch', { placeholder: 'title' });
const tools4 = [
  ['pytest', 'テストを集めて実行する本体'],
  ['pytest-asyncio', '時間で動く処理(BGP など)'],
  ['pytest-cov', 'テストがコードのどこまでを通ったか'],
  ['TestClient', '起動せずに API を呼ぶ'],
  ['ruff', 'コードの書き方チェック'],
];
txt(s, 'テストの道具(CI も同じ一式を入れる)', { x: 0.5, y: 1.15, w: 5.2, h: 0.35, fontSize: 13, bold: true, color: C.accent1 });
tools4.forEach(([h, b], i) => {
  const y = 1.6 + i * 0.66;
  hex(s, 0.5, y, 0.42, C.accent1, '', 'tool hex ' + (i + 1));
  txt(s, h, { x: 1.05, y: y - 0.02, w: 1.65, h: 0.4, fontSize: 13, bold: true, color: C.background1, valign: 'middle' });
  txt(s, b, { x: 2.75, y: y - 0.02, w: 3.0, h: 0.4, fontSize: 11, valign: 'middle' });
});
card(s, 6.0, 1.15, 3.5, 3.85, 'torch card');
hex(s, 6.25, 1.35, 0.5, C.accent4, '', 'torch hex');
txt(s, 'PyTorch', { x: 6.9, y: 1.37, w: 2.4, h: 0.42, fontSize: 16, bold: true, color: C.background1, valign: 'middle' });
txt(s, [
  { text: 'テストの道具ではなく、異常検知(CPU などの「いつもと違う」)に使う側', options: { bullet: true, breakLine: true } },
  { text: '配布元に届かないため、手元で落としたものを GitHub の Release に置いて入れた', options: { bullet: true, breakLine: true } },
  { text: '本体には不要。入っていなければテスト 8 件を飛ばす', options: { bullet: true, breakLine: true } },
  { text: '既定は NumPy 版で、こちらは毎回テストしている', options: { bullet: true } },
], { x: 6.25, y: 2.0, w: 3.05, h: 2.9, fontSize: 11, paraSpaceAfter: 6 });
s.addNotes('テストの道具は pytest が中心で、時間で動く処理には pytest-asyncio、どこまでテストが通ったかは pytest-cov、サーバーを起動せずに API を呼ぶには FastAPI の TestClient、書き方のチェックに ruff を使います。CI も同じ一式を入れています。PyTorch はテストの道具ではなく、異常検知に使うテストされる側のライブラリです。サンドボックスからは配布元に届かないため、手元で落としたファイルを GitHub の Release 経由で入れる回り道をしました。本体には不要なので、入っていない環境ではそのテストを飛ばします。');

// 5. 数字
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('今日の全体テスト', { placeholder: 'title' });
const stats = [
  ['2,134', 'passed / 失敗 0', C.accent5],
  ['17 分', '全体テストの所要時間', C.accent1],
  ['172', 'テストファイル', C.accent4],
  ['62', 'シナリオの確認項目', C.accent2],
];
stats.forEach(([n, l, col], i) => {
  const x = 0.5 + i * 2.3;
  card(s, x, 1.35, 2.1, 2.4, 'stat card ' + (i + 1));
  txt(s, n, { x: x + 0.15, y: 1.65, w: 1.8, h: 1.0, align: 'center', valign: 'middle', fontSize: 40, bold: true, color: col });
  txt(s, l, { x: x + 0.15, y: 2.8, w: 1.8, h: 0.7, align: 'center', fontSize: 12, color: C.background2 });
});
txt(s, '新しく足したテスト: gNMI のカウンタ 5 件・ポートカウンタ 12 件・STP 3 件', { x: 0.5, y: 4.2, w: 9, h: 0.4, fontSize: 14, color: C.background1 });
s.addNotes('今日の全体テストは 2,134 件が通り、失敗は 0 件でした。所要時間は約 17 分です。今日の作業で新しく足したテストは、gNMI のカウンタ、ポートカウンタ、STP の合わせて 20 件です。');

// 6. 本物のツール
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('本物のツールを当てて確かめる', { placeholder: 'title' });
const H = { bold: true, color: C.text1, fill: { color: C.accent1 }, fontSize: 13 };
const R = (a, b, c) => [{ text: a, options: { bold: true, color: C.background1 } }, { text: b }, { text: c, options: { color: C.accent2 } }];
s.addTable([
  [{ text: 'ツール', options: H }, { text: '確かめたこと', options: H }, { text: '結果', options: H }],
  R('snmpwalk / snmpget', '待機中のアップリンクの IF-MIB', 'BPDU が 2 秒ごとに増加'),
  R('snmpget', 'PC から ping 10 回(1000 B)', 'ユニキャスト 10 個・10,280 B'),
  R('gnmic', 'gNMI の 1 秒ストリーム', '1 秒ごとに届き、ping の分だけ増加'),
  R('Playwright', '/cyber-view の演出', '起動演出と FX の切り替え、エラー 0'),
  R('uvicorn + curl', '本物のサーバーに CLI を流す', 'show の出力と API の値が一致'),
  R('pyATS / Genie', 'show の出力を実機用の解析器で読む', '読めない形を見つけて直した(以前)'),
  R('Batfish / GoBGP', '経路計算と本物の BGP と突き合わせ', '食い違いを見つけて直した(以前)'),
], { x: 0.5, y: 1.25, w: 9.0, colW: [2.1, 3.4, 3.5], fontSize: 12, color: C.background2, fill: { color: C.text2 }, border: { type: 'solid', pt: 1, color: C.text1 }, rowH: 0.45, valign: 'middle', objectName: 'tools table' });
s.addNotes('単体テストは作り物の中で完結するので、実機の監視ツールから見て正しいかは別に確かめます。今日は net-snmp の snmpwalk、gnmic、Playwright を使いました。');

// 6b. 試験の全体像
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('試験の全体像: 作り物の中と、外から本物で', { placeholder: 'title' });
const OV = { bold: true, color: C.text1, fill: { color: C.accent1 }, fontSize: 12 };
const ov = (a, b, c, inside) => [{ text: a, options: { bold: true, color: C.background1 } }, { text: b },
  { text: c, options: { color: inside ? C.accent1 : C.accent2 } }];
s.addTable([
  [{ text: '試験', options: OV }, { text: '何を確かめるか', options: OV }, { text: '道具', options: OV }],
  ov('単体・結合', '1 つ 1 つの部品が正しく動くか', 'pytest(約 2,160 件)', true),
  ov('シナリオ回帰', '本物のサーバーで OSPF・BGP・STP が通しで動くか', 'scenario_regression(12 本)', true),
  ov('機種×プロトコル', '3 機種 × 4 プロトコルの正常・障害・復旧・フラップ', '総当たり 144 項目', true),
  ov('LLM による判定', '文字列の一致では書きにくい「意味として正しいか」', 'llm_judge', true),
  ov('本物のツール', '監視・自動化ツールから見て正しいか', 'snmpwalk・gnmic・pyATS など', false),
  ov('ルートインジェクター', '外から本物の RIP / BGP を打ち込み、受け取って広めるか', 'route_injector_cli', false),
  ov('実環境', '自宅の本物の Catalyst で SNMP・trap・応答なし', '単体版 + 実機', false),
  ov('CI', '「自分の環境では通る」を防ぐ', 'GitHub Actions', false),
], { x: 0.5, y: 1.15, w: 9.0, colW: [1.9, 4.4, 2.7], fontSize: 11, color: C.background2, fill: { color: C.text2 },
     border: { type: 'solid', pt: 1, color: C.text1 }, rowH: 0.39, valign: 'middle', objectName: 'overview table' });
txt(s, [{ text: '水色', options: { color: C.accent1, bold: true } }, { text: ' = 作り物の中で確かめる   ' },
        { text: '橙', options: { color: C.accent2, bold: true } }, { text: ' = 外から本物で叩く。片方で漏れたバグをもう片方が拾う' }],
    { x: 0.5, y: 4.82, w: 9, h: 0.3, fontSize: 11 });
s.addNotes('試験は大きく 8 種類あります。上の 4 つは作り物の中で確かめる試験、下の 4 つは外から本物で叩く試験です。今日の STP の 2 件も、単体試験は通っていて、本物のツールで数値を見て初めて見つかりました。両方あることで漏れが減ります。');

// 6c. 画面で見る試験の様子
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'テスト' });
s.addText('試験の様子をニルヴァーナの画面で見る', { placeholder: 'title' });
s.addImage({ path: 'docs/images/cyber-view-test-run.jpg', x: 0.5, y: 1.15, w: 6.2, h: 3.68, objectName: 'test run screenshot',
             altText: 'ニルヴァーナの ROUTER LINKS 画面。装置の上に紫の「試」・緑の PASS・赤の「警」のプレート、左下に TEST RUN パネル' });
const legend = [
  ['試(紫)', 'どの装置に何を当てたか', C.accent4],
  ['試(緑)', 'チェックが通った', C.accent5],
  ['警(赤)', 'チェックが落ちた。理由も出す', C.accent3],
  ['TEST RUN', '件数と直近の結果。終了後 60 秒で消える', C.accent1],
];
legend.forEach(([h, b, col], i) => {
  const y = 1.2 + i * 0.9;
  hex(s, 6.95, y, 0.42, col, '', 'legend hex ' + (i + 1));
  txt(s, h, { x: 7.5, y: y - 0.03, w: 2.0, h: 0.32, fontSize: 13, bold: true, color: C.background1 });
  txt(s, b, { x: 7.5, y: y + 0.28, w: 2.0, h: 0.55, fontSize: 10 });
});
s.addNotes('Claude が裏で回している試験を、ニルヴァーナの画面でそのまま見られるようにしました。シナリオ回帰を流すと、装置の上にどのコマンドを当てたかが紫の「試」で、通ったら緑、落ちたら赤の「警」で出ます。左下の TEST RUN パネルには件数と直近の結果が流れます。pytest も環境変数 NETLAB_ACTIVITY_URL を指定すれば同じように流せます。');

// 7. CI が落ちたとき
pres.addSection({ title: '失敗への対応' });
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '失敗への対応' });
s.addText('CI が落ちたら、再実行ではなく原因を探す', { placeholder: 'title' });
const cases = [
  ['L2 ループ', '裏の定期処理が、テストより先に警報を出していた', 'テスト中は定期処理を止め、検知を直接呼ぶ', C.accent2],
  ['STP の復旧ログ', 'no shutdown の途中で BPDU がポートを作り直し、相手側の up ログが出ない', 'エミュレータの実バグ。落としたポートは復旧処理だけが作り直す', C.accent3],
  ['RIP のシナリオ', 'フィルタより先に経路が隣へ届くことがあった', 'distribute-list を network の前に入れる', C.accent2],
];
cases.forEach(([h, cause, fix, col], i) => {
  const x = 0.5 + i * 3.05;
  card(s, x, 1.25, 2.85, 3.65, 'case card ' + (i + 1));
  hex(s, x + 0.25, 1.45, 0.5, col, i === 1 ? '警' : '注', 'case hex ' + (i + 1), i === 1 ? C.background1 : C.text1);
  txt(s, h, { x: x + 0.9, y: 1.5, w: 1.8, h: 0.45, fontSize: 15, bold: true, color: C.background1, valign: 'middle' });
  txt(s, '原因', { x: x + 0.25, y: 2.15, w: 2.4, h: 0.3, fontSize: 11, bold: true, color: C.accent6 });
  txt(s, cause, { x: x + 0.25, y: 2.45, w: 2.4, h: 1.05, fontSize: 12 });
  txt(s, '直し方', { x: x + 0.25, y: 3.5, w: 2.4, h: 0.3, fontSize: 11, bold: true, color: C.accent6 });
  txt(s, fix, { x: x + 0.25, y: 3.8, w: 2.4, h: 1.0, fontSize: 12, color: C.background1 });
});
s.addNotes('今日、GitHub の CI で落ちたのは 3 件です。どれも「時々落ちる」で片付けず、原因を突き止めました。2 件目の STP は、テストではなくエミュレータ自体の実バグでした。');

// 8. 再現してから直す
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '失敗への対応' });
s.addText('まず同じ失敗を手元で再現する', { placeholder: 'title' });
card(s, 0.5, 1.25, 4.35, 3.2, 'before card');
card(s, 5.15, 1.25, 4.35, 3.2, 'after card');
txt(s, '直す前のコード', { x: 0.8, y: 1.45, w: 3.8, h: 0.4, fontSize: 16, bold: true, color: C.accent3 });
txt(s, '「ループを閉じた直後に 2.5 秒待つ」をテストに一時的に足すと、CI と全く同じ失敗が毎回起きる', { x: 0.8, y: 1.95, w: 3.8, h: 1.1, fontSize: 13 });
s.addText('assert (0 == 1)\n1 failed', { x: 0.8, y: 3.2, w: 3.8, h: 0.9, fontFace: 'Courier New', fontSize: 14, color: C.accent3, fill: { color: C.text1 }, margin: 8, isTextBox: true, objectName: 'before output' });
txt(s, '直した後のコード', { x: 5.45, y: 1.45, w: 3.8, h: 0.4, fontSize: 16, bold: true, color: C.accent5 });
txt(s, '同じ 2.5 秒の待ちを入れても通る。待ちを外して全体テストと CI でもう一度確かめる', { x: 5.45, y: 1.95, w: 3.8, h: 1.1, fontSize: 13 });
s.addText('29 passed', { x: 5.45, y: 3.2, w: 3.8, h: 0.9, fontFace: 'Courier New', fontSize: 14, color: C.accent5, fill: { color: C.text1 }, margin: 8, isTextBox: true, objectName: 'after output' });
txt(s, '再現できない修正は「たまたま通った」と区別がつかない', { x: 0.5, y: 4.65, w: 9, h: 0.35, fontSize: 13, italic: true, color: C.accent2 });
s.addNotes('時々しか落ちないテストは、落ちる条件を意図的に作って再現します。今回は待ち時間を 2.5 秒足すと、CI と同じ assert (0 == 1) が毎回出ました。直した後は同じ条件でも通ることを確かめています。');

// 9. 見つかった実バグ
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '失敗への対応' });
s.addText('確かめる中で見つかった実バグ(今日の分)', { placeholder: 'title' });
const bugs = [
  ['STP', 'SVI のサブネットが違うと BPDU が届かず、2 台とも「ルート」のまま'],
  ['STP', 'no shutdown で戻したとき、相手側の up ログが出ず LINK DOWN が残る'],
  ['カウンタ', '1/30 を 60 回足すと 1.999… になり、LLDP を 1 個少なく数えていた'],
  ['SNMP', 'L2 のアクセスポートが IF-MIB に出ず、流量を読めなかった'],
  ['PC', 'ping -c / -s でカウンタが動かず、ifconfig で IP が設定されなかった'],
  ['単体版', 'linkUp の trap を取りこぼすと、LINK DOWN が消えない'],
];
bugs.forEach(([tag, b], i) => {
  const col = i % 2, row = Math.floor(i / 2);
  const x = 0.5 + col * 4.6, y = 1.25 + row * 1.2;
  hex(s, x, y, 0.55, C.accent3, '!', 'bug hex ' + (i + 1), C.background1);
  txt(s, tag, { x: x + 0.7, y: y + 0.0, w: 3.6, h: 0.3, fontSize: 12, bold: true, color: C.accent3 });
  txt(s, b, { x: x + 0.7, y: y + 0.3, w: 3.7, h: 0.8, fontSize: 12 });
});
s.addNotes('作った機能を本物のツールで確かめると、関係する別の場所のバグも見つかります。今日見つけて直した実バグがこの 6 件です。STP の 1 件目は、SNMP のカウンタを確かめている最中に気づきました。');

// 9b. ある 1 日の実況
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '失敗への対応' });
s.addText('ある 1 日の実況(2026-11-20)', { placeholder: 'title' });
const day = [
  ['CI', '午前の PR で、GitHub の試験が 3 件落ちる', C.accent3],
  ['再現して直す', '手元で同じ失敗を再現。うち 1 件は STP の実バグ(復旧ログが出ない)', C.accent3],
  ['依頼', '「各機器のポートの SNMP カウンタを」', C.accent1],
  ['作る・確かめる', '全体 2,134 件が通る。snmpwalk で「STP が 2 台ともルート」に気づく', C.accent2],
  ['原因', 'BPDU が「同じ IP サブネットか」の判定に入っていた。外して直す', C.accent3],
  ['取り込み', '緑を確かめて main へ。この資料にまとめる', C.accent5],
];
day.forEach(([h, b, col], i) => {
  const y = 1.12 + i * 0.64;
  hex(s, 0.6, y, 0.48, col, String(i + 1), 'day hex ' + (i + 1), col === C.accent3 ? C.background1 : C.text1);
  txt(s, h, { x: 1.25, y: y + 0.04, w: 1.75, h: 0.35, fontSize: 14, bold: true, color: C.background1 });
  txt(s, b, { x: 3.0, y: y + 0.04, w: 6.5, h: 0.35, fontSize: 12 });
});
s.addNotes('今日 1 日の流れです。午前は、PR の GitHub の試験が 3 件落ちました。手元で同じ失敗を再現して直し、うち 1 件は STP の復旧ログが出ないエミュレータの実バグでした。午後はポートの SNMP カウンタを頼まれて実装し、全体試験も通りました。ところが本物の snmpwalk で数値を見ていると、STP が 2 台ともルートのままになっていることに気づきました。原因は、L2 の BPDU が IP のサブネット判定に入っていたことでした。直して緑を確かめ、main に取り込みました。');

// 9c. 知らないうちに CI/CD
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: '失敗への対応' });
s.addText('知らないうちに CI/CD を回している', { placeholder: 'title' });
const cicdFlow = [
  ['人', '「やって」', C.accent2],
  ['Claude', '作業ブランチに push', C.accent1],
  ['CI', 'GitHub が全試験を自動で', C.accent5],
  ['人', '「取り込んで」', C.accent2],
  ['CD', 'main を Render が公開', C.accent4],
];
cicdFlow.forEach(([who, what, col], i) => {
  const x = 0.55 + i * 1.86;
  hex(s, x + 0.35, 1.15, 0.62, col, String(i + 1), 'flow hex ' + (i + 1));
  txt(s, who, { x, y: 1.75, w: 1.32, h: 0.3, fontSize: 13, bold: true, color: C.background1, align: 'center' });
  txt(s, what, { x, y: 2.05, w: 1.32, h: 0.5, fontSize: 11, align: 'center' });
  if (i < cicdFlow.length - 1) {
    s.addShape(SH.RIGHT_ARROW, { x: x + 1.38, y: 1.38, w: 0.4, h: 0.22, fill: { color: C.accent6 }, line: { color: C.accent6, width: 0 }, objectName: 'flow arrow ' + (i + 1) });
  }
});
card(s, 0.5, 2.75, 4.35, 2.05, 'ci card');
card(s, 5.15, 2.75, 4.35, 2.05, 'cd card');
txt(s, 'CI(継続的インテグレーション)', { x: 0.75, y: 2.9, w: 3.9, h: 0.35, fontSize: 14, bold: true, color: C.accent5 });
txt(s, '変更を小さく何度も混ぜ、そのたびに自動で全部試す。ここでは push のたびに GitHub Actions が pytest 約 2,200 件とシナリオ回帰を回す(約 20 分)。赤なら直すまで取り込まない', { x: 0.75, y: 3.3, w: 3.9, h: 1.4, fontSize: 11.5 });
txt(s, 'CD(継続的デリバリー)', { x: 5.4, y: 2.9, w: 3.9, h: 0.35, fontSize: 14, bold: true, color: C.accent4 });
txt(s, '試験に通ったものを、いつでも出せる状態に保って出す。ここでは main を常に緑に保ち、Render の公開版(network-lab-emulator.onrender.com)が main から作られる', { x: 5.4, y: 3.3, w: 3.9, h: 1.4, fontSize: 11.5 });
txt(s, '例: 設定例を変えたのに試験を直し忘れた push を、CI が赤で止めた(11/21)', { x: 0.75, y: 4.2, w: 3.9, h: 0.5, fontSize: 11, color: C.accent5 });
txt(s, '例: 赤いコミットのある PR は、緑になるまで「取り込んで」を待つ', { x: 5.4, y: 4.2, w: 3.9, h: 0.5, fontSize: 11, color: C.accent4 });
txt(s, '用語を知らなくても、「やって」と「取り込んで」の 2 つの言葉が CI/CD のスイッチになっている', { x: 0.5, y: 4.88, w: 9, h: 0.3, fontSize: 12, italic: true, color: C.accent2 });
s.addNotes('CI/CD という言葉を意識しなくても、この開発は CI/CD の形で回っています。人が「やって」と言うと、Claude が作業ブランチに push し、そのたびに GitHub Actions が約 2,200 件の試験とシナリオ回帰を自動で流します。これが CI です。緑になってから人が「取り込んで」と言うと main に入り、公開版は main から作られます。これが CD です。人は用語を知らなくても、2 つの言葉で承認の関門を握っています。');

// 10. 役割分担
pres.addSection({ title: 'まとめ' });
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'まとめ' });
s.addText('人が決めること、Claude が進めること', { placeholder: 'title' });
card(s, 0.5, 1.25, 4.35, 3.6, 'human card');
card(s, 5.15, 1.25, 4.35, 3.6, 'claude card');
hex(s, 0.8, 1.45, 0.55, C.accent2, '人', 'human hex');
hex(s, 5.45, 1.45, 0.55, C.accent1, 'AI', 'claude hex');
txt(s, '人', { x: 1.5, y: 1.5, w: 3, h: 0.45, fontSize: 18, bold: true, color: C.background1, valign: 'middle' });
txt(s, 'Claude', { x: 6.15, y: 1.5, w: 3, h: 0.45, fontSize: 18, bold: true, color: C.background1, valign: 'middle' });
const humanList = ['何を作るか・優先順位を決める', '画面の見た目を判断する', '自宅の実機(Catalyst)で試す', '「main に取り込んで」と決める'];
const claudeList = ['コードを読み、実装し、テストを書く', '本物のツールで動きを確かめる', 'CI の失敗の原因を探して直す', 'ドキュメントと変更まとめを書く'];
txt(s, humanList.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < humanList.length - 1 } })), { x: 0.8, y: 2.25, w: 3.8, h: 2.4, fontSize: 14, paraSpaceAfter: 8 });
txt(s, claudeList.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < claudeList.length - 1 } })), { x: 5.45, y: 2.25, w: 3.8, h: 2.4, fontSize: 14, paraSpaceAfter: 8 });
s.addNotes('人が決めるのは方向と判断です。何を作るか、見た目はどうか、自宅の実機で試すか、main に取り込むか。手を動かして確かめる部分は Claude が進めます。');

// 11. 安全装置
s = pres.addSlide({ masterName: 'CONTENT_DARK', sectionTitle: 'まとめ' });
s.addText('勝手に進めすぎないための決まり', { placeholder: 'title' });
const rules = [
  ['main への取り込みは、人が頼んだときだけ', 'CI が緑でも、指示が無ければマージしない'],
  ['履歴を書き換えない', '強制 push が止められたときは、普通のマージで対応した'],
  ['秘密はコミットしない', 'API キーは環境変数。証明書は .gitignore'],
  ['テストを消して緑にしない', '落ちたテストは原因を直す。スキップや無効化はしない'],
];
rules.forEach(([h, b], i) => {
  const y = 1.25 + i * 0.9;
  hex(s, 0.5, y, 0.6, C.accent5, String(i + 1), 'rule hex ' + (i + 1));
  txt(s, h, { x: 1.3, y: y + 0.02, w: 8, h: 0.35, fontSize: 15, bold: true, color: C.background1 });
  txt(s, b, { x: 1.3, y: y + 0.38, w: 8, h: 0.4, fontSize: 12 });
});
s.addNotes('AI に任せる範囲が広いほど、決まりが大事です。今日も、強制 push は環境側で止められ、通常のマージで対応しました。main への取り込みは「取り込んで」と言われたときだけ行います。');

// 12. 次の一手
s = pres.addSlide({ masterName: 'TITLE_DARK', sectionTitle: 'まとめ' });
s.addText('次の一手', { placeholder: 'title' });
s.addText([
  { text: 'gNMI の 1 秒カウンタをニルヴァーナの画面へ', options: { bullet: true, breakLine: true } },
  { text: 'Streamlit でカウンタ履歴のページ', options: { bullet: true, breakLine: true } },
  { text: '全体テストの並列化で 17 分を短く', options: { bullet: true } },
], { placeholder: 'body', paraSpaceAfter: 6 });
hex(s, 7.75, 0.45, 0.9, C.accent1, '', 'motif hex 1');
hex(s, 8.55, 0.95, 0.65, C.accent2, '', 'motif hex 2');
hex(s, 7.95, 1.25, 0.5, C.accent3, '', 'motif hex 3');
s.addNotes('次は、今日作った gNMI の 1 秒カウンタをニルヴァーナの画面に取り込むこと、Streamlit でカウンタ履歴のページを作ること、全体テストを並列化して 17 分を短くすることを考えています。');

(async () => {
  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log('wrote', OUT);
})();
