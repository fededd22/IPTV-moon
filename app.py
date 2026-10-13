"""
============================================================
  IPTV Server - Flask + CORS Proxy
============================================================
  - يستضيف صفحة HTML مباشرة من نفس الملف
  - يوفر proxy لتجاوز CORS مع قوائم M3U و streams
  - يدعم Range requests و HLS (m3u8)
============================================================
"""

import os
import re
import time
import logging
from urllib.parse import urlparse, urljoin, quote, unquote
from flask import Flask, request, Response, jsonify, render_template_string, send_from_directory
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== الإعدادات ==========
PORT = int(os.environ.get("PORT", 3000))
HOST = os.environ.get("HOST", "0.0.0.0")
TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s")
logger = logging.getLogger("iptv")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ========== Session HTTP موحّد ==========
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount("http://", HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50))
session.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50))

# ========== حماية SSRF ==========
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}

def is_host_blocked(hostname: str) -> bool:
    if not hostname:
        return True
    hostname = hostname.lower()
    if hostname in BLOCKED_HOSTS:
        return True
    if hostname.startswith("192.168.") or hostname.startswith("10."):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", hostname):
        return True
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        return True
    return False


# ============================================================
#   الصفحة الرئيسية (HTML مدمج مباشرة)
# ============================================================
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>مشغل IPTV المتقدم</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background: #0f0f1a; color: #fff;
         min-height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  header { background: linear-gradient(90deg, #1e1e2f, #2a2a45); padding: 10px 20px;
           display: flex; justify-content: space-between; align-items: center;
           box-shadow: 0 2px 10px rgba(0,0,0,0.5); flex-wrap: wrap; gap: 10px; z-index: 100; }
  header h1 { font-size: 18px; background: linear-gradient(90deg, #00d4ff, #7b2ff7);
              -webkit-background-clip: text; -webkit-text-fill-color: transparent; white-space: nowrap; }
  .header-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .btn { padding: 8px 16px; border-radius: 20px; border: none;
         background: linear-gradient(90deg, #00d4ff, #7b2ff7); color: #fff; cursor: pointer;
         font-weight: bold; font-size: 13px; transition: 0.2s; white-space: nowrap; }
  .btn:hover { transform: scale(1.05); }
  .btn.secondary { background: #2a2a45; }
  .container { display: flex; flex: 1; overflow: hidden; height: calc(100vh - 60px); }
  .sidebar { width: 360px; background: #15152a; display: flex; flex-direction: column;
             border-left: 1px solid #2a2a45; }
  .sidebar-tools { padding: 10px; border-bottom: 1px solid #2a2a45;
                   display: flex; flex-direction: column; gap: 8px; }
  .search-box input { width: 100%; padding: 9px 15px; border-radius: 20px; border: 1px solid #333;
                      background: #1a1a2e; color: #fff; outline: none; font-size: 13px; transition: 0.3s; }
  .search-box input:focus { border-color: #00d4ff; }
  .filters { display: flex; gap: 6px; align-items: center; }
  .filters select { flex: 1; padding: 8px 12px; border-radius: 15px; border: 1px solid #333;
                    background: #1a1a2e; color: #fff; font-size: 12px; outline: none; cursor: pointer; }
  .filters .count { font-size: 11px; color: #888; white-space: nowrap; }
  .channel-list { flex: 1; overflow-y: auto; }
  .channel-list::-webkit-scrollbar { width: 8px; }
  .channel-list::-webkit-scrollbar-track { background: #15152a; }
  .channel-list::-webkit-scrollbar-thumb { background: #7b2ff7; border-radius: 4px; }
  .group-header { padding: 8px 15px; background: #1a1a30; font-size: 12px; font-weight: bold;
                  color: #00d4ff; position: sticky; top: 0; z-index: 5;
                  border-bottom: 1px solid #2a2a45; display: flex; justify-content: space-between; }
  .group-header span:last-child { color: #666; font-weight: normal; }
  .channel { padding: 10px 15px; cursor: pointer; display: flex; align-items: center; gap: 10px;
             border-bottom: 1px solid #1f1f38; transition: 0.2s; }
  .channel:hover { background: #1f1f3a; }
  .channel.active { background: linear-gradient(90deg, #7b2ff7, #00d4ff); }
  .channel img { width: 36px; height: 36px; object-fit: contain; background: #fff;
                 border-radius: 6px; padding: 3px; flex-shrink: 0; }
  .channel-info { flex: 1; overflow: hidden; }
  .channel-info .name { font-size: 13px; font-weight: 600; white-space: nowrap;
                        overflow: hidden; text-overflow: ellipsis; }
  .channel-info .group { font-size: 10px; opacity: 0.6; white-space: nowrap;
                         overflow: hidden; text-overflow: ellipsis; }
  .player-area { flex: 1; display: flex; flex-direction: column; background: #000; }
  .video-wrapper { flex: 1; position: relative; background: #000; }
  video { width: 100%; height: 100%; background: #000; }
  .loading-overlay { position: absolute; inset: 0; display: none; justify-content: center;
                     align-items: center; flex-direction: column; gap: 15px;
                     background: rgba(0,0,0,0.7); z-index: 10; }
  .loading-overlay.show { display: flex; }
  .spinner { width: 50px; height: 50px; border: 4px solid #333; border-top-color: #00d4ff;
             border-radius: 50%; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .welcome { position: absolute; inset: 0; display: flex; justify-content: center;
             align-items: center; flex-direction: column; gap: 15px; color: #666;
             text-align: center; padding: 20px; }
  .welcome svg { width: 80px; height: 80px; opacity: 0.3; }
  .now-playing { padding: 10px 20px; background: #15152a; border-top: 1px solid #2a2a45;
                 display: flex; justify-content: space-between; align-items: center; gap: 15px; }
  .now-playing .title { font-size: 14px; font-weight: bold; }
  .now-playing .group { font-size: 11px; opacity: 0.7; }
  .status { padding: 6px 14px; border-radius: 20px; font-size: 12px; background: #2a2a45; white-space: nowrap; }
  .status.live { background: #e74c3c; animation: pulse 1.5s infinite; }
  .status.loading { background: #f39c12; }
  .status.error { background: #c0392b; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.8); display: none;
              justify-content: center; align-items: center; z-index: 1000; padding: 20px; }
  .modal-bg.show { display: flex; }
  .modal { background: #1a1a2e; border-radius: 15px; padding: 25px; max-width: 500px;
           width: 100%; border: 1px solid #2a2a45; }
  .modal h2 { font-size: 18px; margin-bottom: 15px; color: #00d4ff; }
  .modal label { display: block; font-size: 13px; margin-bottom: 6px; color: #aaa; }
  .modal input[type="text"], .modal input[type="url"], .modal textarea {
    width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid #333;
    background: #0f0f1a; color: #fff; outline: none; font-size: 13px;
    margin-bottom: 12px; font-family: inherit; }
  .modal textarea { min-height: 90px; resize: vertical; }
  .modal input:focus, .modal textarea:focus { border-color: #00d4ff; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 10px; }
  .progress-info { font-size: 12px; color: #00d4ff; margin-top: 10px; text-align: center; }
  .sources-list { margin-top: 15px; max-height: 200px; overflow-y: auto;
                  border-top: 1px solid #2a2a45; padding-top: 10px; }
  .source-item { display: flex; justify-content: space-between; align-items: center;
                 padding: 8px 10px; background: #0f0f1a; border-radius: 8px;
                 margin-bottom: 6px; font-size: 12px; gap: 10px; }
  .source-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .source-item button { background: #c0392b; border: none; color: #fff; padding: 4px 10px;
                        border-radius: 6px; cursor: pointer; font-size: 11px; }
  @media (max-width: 768px) {
    .container { flex-direction: column; height: auto; }
    .sidebar { width: 100%; height: 45vh; border-left: none; border-bottom: 1px solid #2a2a45; }
    .player-area { height: 55vh; }
    header h1 { font-size: 15px; }
    .btn { font-size: 11px; padding: 6px 12px; }
  }
</style>
</head>
<body>

<header>
  <h1>📺 مشغل IPTV (Python Server)</h1>
  <div class="header-actions">
    <button class="btn" onclick="openAddModal()">➕ إضافة رابط IPTV</button>
    <button class="btn secondary" onclick="openManageModal()">⚙️ المصادر</button>
    <button class="btn secondary" onclick="loadAllSources()">🔄 تحديث الكل</button>
  </div>
</header>

<div class="container">
  <div class="sidebar">
    <div class="sidebar-tools">
      <div class="search-box">
        <input type="text" id="searchInput" placeholder="🔍 ابحث عن قناة أو بلد...">
      </div>
      <div class="filters">
        <select id="countryFilter"><option value="__all__">🌍 جميع البلدان</option></select>
        <span class="count" id="countInfo">0 قناة</span>
      </div>
      <div class="filters">
        <select id="groupFilter"><option value="__all__">📁 جميع الفئات</option></select>
      </div>
    </div>
    <div class="channel-list" id="channelList">
      <div style="padding:20px;text-align:center;color:#888;">جاري تحميل القنوات...</div>
    </div>
  </div>

  <div class="player-area">
    <div class="video-wrapper">
      <video id="video" controls autoplay playsinline></video>
      <div class="loading-overlay" id="loading"><div class="spinner"></div><div>جاري التحميل...</div></div>
      <div class="welcome" id="welcome">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z"/></svg>
        <div>اختر قناة من القائمة لبدء المشاهدة</div>
      </div>
    </div>
    <div class="now-playing">
      <div>
        <div class="title" id="nowTitle">لا توجد قناة قيد التشغيل</div>
        <div class="group" id="nowGroup"></div>
      </div>
      <div class="status" id="status">في الانتظار</div>
    </div>
  </div>
</div>

<div class="modal-bg" id="addModal">
  <div class="modal">
    <h2>➕ إضافة رابط IPTV جديد</h2>
    <label>اسم المصدر (اختياري)</label>
    <input type="text" id="sourceName" placeholder="مثال: قنواتي المفضلة">
    <label>رابط M3U أو محتوى القائمة</label>
    <textarea id="sourceUrl" placeholder="https://example.com/playlist.m3u&#10;أو الصق محتوى M3U هنا مباشرة..."></textarea>
    <div class="progress-info" id="addProgress"></div>
    <div class="modal-actions">
      <button class="btn secondary" onclick="closeAddModal()">إلغاء</button>
      <button class="btn" onclick="addSource()">إضافة وتشغيل</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="manageModal">
  <div class="modal">
    <h2>⚙️ إدارة المصادر</h2>
    <div class="sources-list" id="sourcesList"></div>
    <div class="modal-actions">
      <button class="btn secondary" onclick="closeManageModal()">إغلاق</button>
    </div>
  </div>
</div>

<script>
/* ============================================================
   ⚙️ PROXY BASE - يستخدم نفس الخادم (Python Flask)
   ============================================================ */
const PROXY_BASE = window.location.origin;

const DEFAULT_SOURCES = [
  { name: 'القائمة الرئيسية iptv-org', url: 'https://iptv-org.github.io/iptv/index.m3u', permanent: true }
];

const STORAGE_KEY = 'iptv_sources_v2';

const video = document.getElementById('video');
const channelList = document.getElementById('channelList');
const searchInput = document.getElementById('searchInput');
const countryFilter = document.getElementById('countryFilter');
const groupFilter = document.getElementById('groupFilter');
const countInfo = document.getElementById('countInfo');
const loadingEl = document.getElementById('loading');
const welcomeEl = document.getElementById('welcome');
const nowTitle = document.getElementById('nowTitle');
const nowGroup = document.getElementById('nowGroup');
const statusEl = document.getElementById('status');
const addProgress = document.getElementById('addProgress');
const sourcesList = document.getElementById('sourcesList');

let allChannels = [];
let filteredChannels = [];
let hls = null;
let currentChannel = null;

const COUNTRY_NAMES = {
  'SA':'السعودية','AE':'الإمارات','EG':'مصر','MA':'المغرب','DZ':'الجزائر',
  'TN':'تونس','LY':'ليبيا','SD':'السودان','JO':'الأردن','LB':'لبنان',
  'SY':'سوريا','IQ':'العراق','PS':'فلسطين','YE':'اليمن','KW':'الكويت',
  'QA':'قطر','BH':'البحرين','OM':'عُمان','MR':'موريتانيا','SO':'الصومال',
  'DJ':'جيبوتي','KM':'جزر القمر','US':'أمريكا','GB':'بريطانيا','FR':'فرنسا',
  'DE':'ألمانيا','IT':'إيطاليا','ES':'إسبانيا','TR':'تركيا','IR':'إيران',
  'PK':'باكستان','IN':'الهند','CN':'الصين','JP':'اليابان','KR':'كوريا',
  'RU':'روسيا','CA':'كندا','AU':'أستراليا','BR':'البرازيل','MX':'المكسيك',
  'NL':'هولندا','BE':'بلجيكا','SE':'السويد','NO':'النرويج','DK':'الدنمارك',
  'FI':'فنلندا','PL':'بولندا','PT':'البرتغال','GR':'اليونان','CH':'سويسرا',
  'AT':'النمسا','IE':'أيرلندا','CZ':'التشيك','RO':'رومانيا','HU':'المجر',
  'UA':'أوكرانيا','IL':'إسرائيل','AF':'أفغانستان','BD':'بنغلاديش',
  'ID':'إندونيسيا','MY':'ماليزيا','TH':'تايلاند','PH':'الفلبين','VN':'فيتنام',
  'ZA':'جنوب أفريقيا','NG':'نيجيريا','KE':'كينيا','GH':'غانا','ET':'إثيوبيا',
  'AR':'الأرجنتين','CL':'تشيلي','CO':'كولومبيا','PE':'بيرو','VE':'فنزويلا',
  'XX':'غير معروف'
};

function getCountryName(code) {
  if (!code || code === 'XX' || code === 'UNDEFINED') return 'غير معروف';
  code = code.toUpperCase();
  return COUNTRY_NAMES[code] || code;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function getStoredSources() {
  try { const d = localStorage.getItem(STORAGE_KEY); return d ? JSON.parse(d) : []; }
  catch { return []; }
}
function saveStoredSources(sources) { localStorage.setItem(STORAGE_KEY, JSON.stringify(sources)); }
function getAllSources() { return [...DEFAULT_SOURCES, ...getStoredSources()]; }

/* ============================================================
   🌐 استخدام proxy الخادم لجلب القوائم
   ============================================================ */
async function fetchViaProxy(url) {
  // نمرر الرابط عبر proxy الخادم لتجاوز CORS
  const proxyUrl = `${PROXY_BASE}/proxy-m3u?url=${encodeURIComponent(url)}`;
  const resp = await fetch(proxyUrl);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return await resp.text();
}

/* ============================================================
   🔀 تحويل رابط البث ليمر عبر proxy (لتشغيل HLS بدون CORS)
   ============================================================ */
function proxifyStreamUrl(url) {
  // إذا كان الرابط نسبياً من proxy الخادم، نتركه
  if (url.startsWith(PROXY_BASE)) return url;
  // نلف الرابط عبر proxy الخادم
  return `${PROXY_BASE}/proxy?url=${encodeURIComponent(url)}`;
}

/* ====== تحليل M3U ====== */
function parseM3U(text, sourceName) {
  const lines = text.split('\n');
  const result = [];
  let current = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    if (line.startsWith('#EXTINF:')) {
      const nameMatch = line.match(/,(.+)$/);
      const logoMatch = line.match(/tvg-logo="([^"]*)"/);
      const groupMatch = line.match(/group-title="([^"]*)"/);
      const countryMatch = line.match(/tvg-country="([^"]*)"/);
      const countryAlt = line.match(/(?:^|\s)country="([^"]*)"/);
      let country = (countryMatch ? countryMatch[1] : (countryAlt ? countryAlt[1] : '')).trim();
      if (!country) {
        const idMatch = line.match(/tvg-id="([^"]*)"/);
        if (idMatch && idMatch[1]) {
          const parts = idMatch[1].split('.');
          if (parts.length > 1 && parts[parts.length-1].length === 2) {
            country = parts[parts.length-1];
          }
        }
      }
      current = {
        name: nameMatch ? nameMatch[1].trim() : 'قناة',
        logo: logoMatch ? logoMatch[1] : '',
        group: groupMatch ? groupMatch[1] : 'غير مصنف',
        country: country || 'XX',
        countryName: getCountryName(country),
        source: sourceName || 'قائمة'
      };
    } else if (!line.startsWith('#') && current) {
      current.url = line;
      result.push(current);
      current = null;
    }
  }
  return result;
}

/* ====== تحميل كل المصادر ====== */
async function loadAllSources() {
  const sources = getAllSources();
  channelList.innerHTML = `<div style="padding:20px;text-align:center;color:#888;">
    جاري تحميل القنوات من ${sources.length} مصدر...
  </div>`;

  const allResults = [];
  for (let i = 0; i < sources.length; i++) {
    const src = sources[i];
    channelList.innerHTML = `<div style="padding:20px;text-align:center;color:#888;">
      تحميل المصدر ${i+1}/${sources.length}: ${escapeHtml(src.name)}...
    </div>`;
    try {
      let text;
      if (src.url.startsWith('http')) {
        text = await fetchViaProxy(src.url);
      } else {
        text = src.url;
      }
      const parsed = parseM3U(text, src.name);
      console.log(`📥 ${src.name}: ${parsed.length} قناة`);
      allResults.push(...parsed);
    } catch (err) {
      console.error(`فشل تحميل ${src.name}:`, err);
    }
  }

  const seen = new Set();
  const unique = [];
  for (const ch of allResults) {
    if (!seen.has(ch.url)) { seen.add(ch.url); unique.push(ch); }
  }

  allChannels = unique;
  populateFilters(allChannels);
  applyFilters();
  console.log(`✅ تم تحميل ${allChannels.length} قناة`);
}

function populateFilters(channels) {
  const countries = new Map();
  channels.forEach(c => {
    const key = c.country || 'XX';
    const name = c.countryName || getCountryName(key);
    if (!countries.has(key)) countries.set(key, { name, count: 0 });
    countries.get(key).count++;
  });
  const sortedCountries = [...countries.entries()].sort((a,b) => b[1].count - a[1].count);
  const currentCountry = countryFilter.value;
  countryFilter.innerHTML = '<option value="__all__">🌍 جميع البلدان</option>';
  sortedCountries.forEach(([code, {name, count}]) => {
    const opt = document.createElement('option');
    opt.value = code; opt.textContent = `${name} (${count})`;
    countryFilter.appendChild(opt);
  });
  if ([...countryFilter.options].some(o => o.value === currentCountry))
    countryFilter.value = currentCountry;

  const groups = new Map();
  channels.forEach(c => {
    const g = c.group || 'غير مصنف';
    groups.set(g, (groups.get(g) || 0) + 1);
  });
  const sortedGroups = [...groups.entries()].sort((a,b) => b[1].count - a[1].count);
  const currentGroup = groupFilter.value;
  groupFilter.innerHTML = '<option value="__all__">📁 جميع الفئات</option>';
  sortedGroups.forEach(([g, count]) => {
    const opt = document.createElement('option');
    opt.value = g; opt.textContent = `${g} (${count})`;
    groupFilter.appendChild(opt);
  });
  if ([...groupFilter.options].some(o => o.value === currentGroup))
    groupFilter.value = currentGroup;
}

function applyFilters() {
  const query = searchInput.value.trim().toLowerCase();
  const countryVal = countryFilter.value;
  const groupVal = groupFilter.value;
  filteredChannels = allChannels.filter(c => {
    if (countryVal !== '__all__' && c.country !== countryVal) return false;
    if (groupVal !== '__all__' && c.group !== groupVal) return false;
    if (query) {
      const hay = (c.name + ' ' + c.group + ' ' + c.countryName + ' ' + c.source).toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });
  renderChannels(filteredChannels);
  countInfo.textContent = `${filteredChannels.length} قناة`;
}

function renderChannels(list) {
  if (!list.length) {
    channelList.innerHTML = '<div style="padding:20px;text-align:center;color:#888;">لا توجد نتائج</div>';
    return;
  }
  const maxDisplay = 800;
  const displayed = list.slice(0, maxDisplay);
  const fragment = document.createDocumentFragment();
  const grouped = new Map();
  displayed.forEach(c => {
    const key = c.countryName || 'غير معروف';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(c);
  });
  const sortedGroups = [...grouped.entries()].sort((a,b) => b[1].length - a[1].length);
  sortedGroups.forEach(([countryName, channels]) => {
    const header = document.createElement('div');
    header.className = 'group-header';
    header.innerHTML = `<span>🌍 ${escapeHtml(countryName)}</span><span>${channels.length}</span>`;
    fragment.appendChild(header);
    channels.forEach(ch => {
      const div = document.createElement('div');
      div.className = 'channel';
      const fallbackLogo = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23666"><rect width="24" height="24" rx="4"/></svg>';
      const logo = ch.logo || fallbackLogo;
      div.innerHTML = `
        <img src="${escapeHtml(logo)}" onerror="this.src='${fallbackLogo}'">
        <div class="channel-info">
          <div class="name">${escapeHtml(ch.name)}</div>
          <div class="group">${escapeHtml(ch.group)} • ${escapeHtml(ch.source)}</div>
        </div>`;
      div.onclick = () => {
        document.querySelectorAll('.channel.active').forEach(e => e.classList.remove('active'));
        div.classList.add('active');
        playChannel(ch);
      };
      fragment.appendChild(div);
    });
  });
  channelList.innerHTML = '';
  channelList.appendChild(fragment);
  if (list.length > maxDisplay) {
    const info = document.createElement('div');
    info.style.cssText = 'padding:12px;text-align:center;color:#888;font-size:12px;';
    info.textContent = `يتم عرض ${maxDisplay} من أصل ${list.length} قناة.`;
    channelList.appendChild(info);
  }
}

/* ====== تشغيل القناة (عبر proxy الخادم) ====== */
function playChannel(ch) {
  currentChannel = ch;
  welcomeEl.style.display = 'none';
  loadingEl.classList.add('show');
  statusEl.textContent = 'جاري التحميل...';
  statusEl.className = 'status loading';
  nowTitle.textContent = ch.name;
  nowGroup.textContent = `${ch.group} • ${ch.countryName} • ${ch.source}`;

  if (hls) { hls.destroy(); hls = null; }
  video.pause();
  video.removeAttribute('src');
  video.load();

  // 🔥 استخدام proxy الخادم لتشغيل القناة
  const url = proxifyStreamUrl(ch.url);

  const isHls = ch.url.includes('.m3u8') || ch.url.includes('m3u8') ||
                ch.url.includes('/hls/') || ch.url.includes('format=m3u8');

  if (Hls.isSupported() && isHls) {
    hls = new Hls({
      enableWorker: true,
      lowLatencyMode: true,
      backBufferLength: 60,
      manifestLoadingTimeOut: 15000,
      manifestLoadingMaxRetry: 3,
      levelLoadingTimeOut: 15000,
      fragLoadingTimeOut: 25000
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(e => console.log('Autoplay blocked:', e)));
    hls.on(Hls.Events.ERROR, (evt, data) => {
      if (data.fatal) {
        console.error('HLS fatal:', data);
        loadingEl.classList.remove('show');
        statusEl.textContent = '❌ لا يعمل';
        statusEl.className = 'status error';
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl') && isHls) {
    video.src = url;
    video.addEventListener('loadedmetadata', () => video.play().catch(()=>{}), { once: true });
  } else {
    video.src = url;
    video.play().catch(e => {
      console.error(e);
      loadingEl.classList.remove('show');
      statusEl.textContent = '❌ تعذر التشغيل';
      statusEl.className = 'status error';
    });
  }
}

video.addEventListener('playing', () => {
  loadingEl.classList.remove('show');
  statusEl.textContent = '● مباشر';
  statusEl.className = 'status live';
});
video.addEventListener('waiting', () => loadingEl.classList.add('show'));
video.addEventListener('error', () => {
  loadingEl.classList.remove('show');
  statusEl.textContent = '❌ خطأ';
  statusEl.className = 'status error';
});

/* ====== إدارة المصادر ====== */
function openAddModal() {
  document.getElementById('sourceName').value = '';
  document.getElementById('sourceUrl').value = '';
  addProgress.textContent = '';
  document.getElementById('addModal').classList.add('show');
}
function closeAddModal() { document.getElementById('addModal').classList.remove('show'); }
function openManageModal() { renderSourcesList(); document.getElementById('manageModal').classList.add('show'); }
function closeManageModal() { document.getElementById('manageModal').classList.remove('show'); }

function renderSourcesList() {
  const stored = getStoredSources();
  sourcesList.innerHTML = '';
  DEFAULT_SOURCES.forEach(s => {
    const div = document.createElement('div');
    div.className = 'source-item';
    div.innerHTML = `<span>🌐 ${escapeHtml(s.name)} <small style="color:#666;">(افتراضي)</small></span>`;
    sourcesList.appendChild(div);
  });
  if (!stored.length) {
    const div = document.createElement('div');
    div.style.cssText = 'text-align:center;color:#666;font-size:12px;padding:10px;';
    div.textContent = 'لا توجد مصادر مضافة';
    sourcesList.appendChild(div);
  } else {
    stored.forEach((s, idx) => {
      const div = document.createElement('div');
      div.className = 'source-item';
      const shortUrl = s.url.startsWith('http') ? s.url : '[محتوى مباشر]';
      div.innerHTML = `
        <span>📺 ${escapeHtml(s.name)}<br><small style="color:#666;font-size:10px;">${escapeHtml(shortUrl.slice(0,60))}</small></span>
        <button onclick="removeSource(${idx})">🗑️ حذف</button>`;
      sourcesList.appendChild(div);
    });
  }
}

function removeSource(idx) {
  if (!confirm('هل تريد حذف هذا المصدر؟')) return;
  const stored = getStoredSources();
  stored.splice(idx, 1);
  saveStoredSources(stored);
  renderSourcesList();
  loadAllSources();
}

async function addSource() {
  const name = document.getElementById('sourceName').value.trim();
  const url = document.getElementById('sourceUrl').value.trim();
  if (!url) {
    addProgress.textContent = '⚠️ الرجاء إدخال رابط';
    addProgress.style.color = '#e74c3c';
    return;
  }
  const sourceName = name || `مصدر ${getStoredSources().length + 1}`;
  addProgress.textContent = '⏳ جاري التحقق...';
  addProgress.style.color = '#00d4ff';
  try {
    let text = url;
    if (url.startsWith('http')) {
      text = await fetchViaProxy(url);
    }
    if (!text.includes('#EXTM3U') && !text.includes('#EXTINF')) {
      addProgress.textContent = '❌ ليست قائمة M3U صالحة';
      addProgress.style.color = '#e74c3c';
      return;
    }
    const parsed = parseM3U(text, sourceName);
    if (!parsed.length) {
      addProgress.textContent = '❌ لا توجد قنوات';
      addProgress.style.color = '#e74c3c';
      return;
    }
    const stored = getStoredSources();
    stored.push({ name: sourceName, url });
    saveStoredSources(stored);
    addProgress.textContent = `✅ تمت الإضافة (${parsed.length} قناة)`;
    addProgress.style.color = '#27ae60';
    setTimeout(() => { closeAddModal(); loadAllSources(); }, 800);
  } catch (err) {
    console.error(err);
    const stored = getStoredSources();
    stored.push({ name: sourceName, url });
    saveStoredSources(stored);
    addProgress.textContent = `⚠️ تمت الإضافة (تحقق فشل: ${err.message})`;
    addProgress.style.color = '#f39c12';
    setTimeout(() => { closeAddModal(); loadAllSources(); }, 1200);
  }
}

searchInput.addEventListener('input', applyFilters);
countryFilter.addEventListener('change', applyFilters);
groupFilter.addEventListener('change', applyFilters);
document.querySelectorAll('.modal-bg').forEach(bg => {
  bg.addEventListener('click', (e) => { if (e.target === bg) bg.classList.remove('show'); });
});

loadAllSources();
</script>
</body>
</html>
"""


# ============================================================
#   Routes
# ============================================================
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "uptime": time.time()})


# ============================================================
#   Proxy M3U - يعيد كتابة روابط القنوات للمرور عبر proxy
# ============================================================
@app.route("/proxy-m3u")
def proxy_m3u():
    target = request.args.get("url")
    if not target:
        return jsonify({"error": "missing url"}), 400

    parsed = urlparse(target)
    if is_host_blocked(parsed.hostname):
        return jsonify({"error": "forbidden host"}), 403

    try:
        r = session.get(target, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=False)
        r.raise_for_status()
        text = r.text

        # إعادة كتابة روابط القنوات للمرور عبر proxy
        proxy_base = f"{request.scheme}://{request.host}/proxy?url="
        lines = text.split("\n")
        output = []
        for line in lines:
            s = line.strip()
            if not s:
                output.append(line)
                continue
            if s.startswith("#"):
                # معالجة URI داخل #EXT-X-KEY
                def repl(m):
                    uri = m.group(1)
                    if uri.startswith("http"):
                        return f'URI="{proxy_base}{quote(uri, safe="")}"'
                    return m.group(0)
                line = re.sub(r'URI="([^"]+)"', repl, line)
                output.append(line)
            else:
                # سطر رابط قناة
                abs_url = urljoin(target, s)
                output.append(f"{proxy_base}{quote(abs_url, safe='')}")
        rewritten = "\n".join(output)

        return Response(
            rewritten,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        logger.error(f"proxy-m3u error: {e}")
        return jsonify({"error": str(e)}), 502


# ============================================================
#   Proxy Stream - للفيديو و HLS
# ============================================================
@app.route("/proxy")
def proxy_stream():
    target = request.args.get("url")
    if not target:
        return jsonify({"error": "missing url"}), 400

    parsed = urlparse(target)
    if is_host_blocked(parsed.hostname):
        return jsonify({"error": "forbidden host"}), 403

    # Headers للتمرير
    headers = {
        "User-Agent": request.headers.get("User-Agent", USER_AGENT),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]
    if "Referer" in request.headers:
        headers["Referer"] = request.headers["Referer"]

    try:
        # stream=True للفيديو (لا نحمل الملف كاملاً)
        is_m3u8_url = target.endswith(".m3u8") or ".m3u8?" in target

        if is_m3u8_url:
            # M3U8 يحتاج معالجة خاصة (إعادة كتابة الروابط)
            r = session.get(target, headers=headers, timeout=TIMEOUT, stream=False)
            r.raise_for_status()
            text = r.text
            proxy_base = f"{request.scheme}://{request.host}/proxy?url="
            base_for_rel = target.rsplit("/", 1)[0] + "/"

            output = []
            for line in text.split("\n"):
                s = line.strip()
                if not s:
                    output.append(line); continue
                if s.startswith("#"):
                    def repl(m):
                        uri = m.group(1)
                        absu = uri if uri.startswith("http") else urljoin(base_for_rel, uri)
                        return f'URI="{proxy_base}{quote(absu, safe="")}"'
                    line = re.sub(r'URI="([^"]+)"', repl, line)
                    output.append(line)
                else:
                    absu = s if s.startswith("http") else urljoin(base_for_rel, s)
                    output.append(f"{proxy_base}{quote(absu, safe='')}")
            return Response(
                "\n".join(output),
                mimetype="application/vnd.apple.mpegurl",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        else:
            # بث مباشر مع دعم Range
            r = session.get(target, headers=headers, timeout=TIMEOUT, stream=True)

            # Headers الاستجابة
            resp_headers = {}
            for h in ["Content-Type", "Content-Length", "Accept-Ranges",
                      "Content-Range", "Cache-Control", "ETag", "Last-Modified"]:
                if h in r.headers:
                    resp_headers[h] = r.headers[h]
            resp_headers["Access-Control-Allow-Origin"] = "*"
            resp_headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"

            def generate():
                try:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            yield chunk
                except Exception as e:
                    logger.error(f"stream chunk error: {e}")

            return Response(
                generate(),
                status=r.status_code,
                headers=resp_headers,
                direct_passthrough=True
            )
    except requests.exceptions.Timeout:
        return jsonify({"error": "upstream timeout"}), 504
    except Exception as e:
        logger.error(f"proxy error: {e}")
        return jsonify({"error": str(e)}), 502


# ============================================================
#   Start
# ============================================================
if __name__ == "__main__":
    logger.info("═══════════════════════════════════════════════")
    logger.info("  📺  IPTV Python Server")
    logger.info("═══════════════════════════════════════════════")
    logger.info(f"  🌍  http://localhost:{PORT}")
    logger.info(f"  🔀  Proxy: /proxy?url=...")
    logger.info(f"  📋  M3U:   /proxy-m3u?url=...")
    logger.info("═══════════════════════════════════════════════")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
