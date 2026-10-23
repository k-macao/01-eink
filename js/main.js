let bleDevice, gattServer;
let epdService, epdCharacteristic;
let currentRotate = 0;
let lastWriteTime = 0;
let timerId = null;

const timeoutSeconds = 60;
const textDecoder = new TextDecoder();

const EpdCmd = {
  INIT:        0x01,
  REFRESH:     0x05,
  SET_TIME:    0x20,
  SET_ROTATE:  0x21,
  WRITE_IMAGE: 0x30,
};

// 图像下发相关状态（MTU / 是否支持 RLE，由设备 notification 上报）
globalThis.EpdLink = globalThis.EpdLink || { mtu: 240, rle: false, reported: false };

const MAX_CONNECT_RETRY = 3;
const CONNECT_RETRY_DELAY_MS = 2000;

function hex2bytes(hex) {
  for (var bytes = [], c = 0; c < hex.length; c += 2)
    bytes.push(parseInt(hex.substr(c, 2), 16));
  return new Uint8Array(bytes);
}

function bytes2hex(data) {
  return new Uint8Array(data).reduce(
    function (memo, i) {
      return memo + ("0" + i.toString(16)).slice(-2);
    }, "");
}

function intToHex(intIn) {
  let stringOut = ("0000" + intIn.toString(16)).substr(-4)
  return stringOut.substring(2, 4) + stringOut.substring(0, 2);
}

function resetVariables() {
  gattServer = null;
  epdService = null;
  epdCharacteristic = null;
  msgIndex = 0;
  document.getElementById("log").value = '';
}

async function writePayload(payload, withResponse = true, silent = false) {
  if (!epdCharacteristic) {
    if (!silent) addLog("服务不可用，请检查蓝牙连接");
    return false;
  }
  const bytes = payload instanceof Uint8Array ? payload : Uint8Array.from(payload);
  if (!silent) addLog(bytes2hex(bytes), '⇑');
  try {
    if (withResponse)
      await epdCharacteristic.writeValueWithResponse(bytes);
    else
      await epdCharacteristic.writeValueWithoutResponse(bytes);
  } catch (e) {
    console.error(e);
    if (e.message) addLog("write: " + e.message);
    return false;
  }
  lastWriteTime = new Date().getTime();
  return true;
}

async function write(cmd, data, withResponse = true) {
  let payload = [cmd];
  if (data) {
    if (typeof data == 'string') data = hex2bytes(data);
    if (data instanceof Uint8Array) data = Array.from(data);
    payload.push(...data)
  }
  return writePayload(payload, withResponse, false);
}

async function syncTime(mode) {
  const timestamp = new Date().getTime() / 1000;
  const data = new Uint8Array([
    (timestamp >> 24) & 0xFF,
    (timestamp >> 16) & 0xFF,
    (timestamp >> 8) & 0xFF,
    timestamp & 0xFF,
    -(new Date().getTimezoneOffset() / 60),
    mode
  ]);
  if (await write(EpdCmd.SET_TIME, data)) {
    addLog("时间已同步！");
    addLog("屏幕刷新完成前请不要操作。");
  }
}

async function rotateScreen() {
  const rotate = (currentRotate === 0) ? 2 : 0;
  await write(EpdCmd.SET_ROTATE, [rotate]);
  currentRotate = rotate;
  addLog("屏幕已旋转180°，正在刷新...");
  syncTime(1);
}

function updateButtonStatus() {
  const connected = gattServer != null && gattServer.connected;
  const status = connected ? null : 'disabled';
  document.getElementById("btn-reconnect").disabled = (gattServer == null || gattServer.connected) ? 'disabled' : null;
  document.getElementById("btn-synctime").disabled = status;
  document.getElementById("btn-rotate").disabled = status;
  const pushBtn = document.getElementById("btn-push-summary");
  if (pushBtn) pushBtn.disabled = status;
}

function disconnect() {
  updateButtonStatus();
  resetVariables();
  addLog('已断开连接.');
  document.getElementById("btn-connect").innerHTML = '连接';
}

async function preConnect() {
  if (gattServer != null && gattServer.connected) {
    if (bleDevice != null && bleDevice.gatt.connected) {
      bleDevice.gatt.disconnect();
    }
  }
  else {
    resetVariables();
    try {
      bleDevice = await navigator.bluetooth.requestDevice({
        optionalServices: ['62750001-d828-918d-fb46-b6c11c675aec'],
        acceptAllDevices: true,
      });
    } catch (e) {
      console.error(e);
      if (e.message) addLog("requestDevice: " + e.message);
      addLog("请检查蓝牙是否已开启，且使用的浏览器支持蓝牙！建议使用以下浏览器：");
      addLog("• 电脑: Chrome/Edge");
      addLog("• Android: Chrome/Edge");
      addLog("• iOS: Bluefy 浏览器");
      return;
    }

    await bleDevice.addEventListener('gattserverdisconnected', disconnect);
    setTimeout(async function () { await connect(); }, 300);
  }
}

async function forceReselect() {
  // 关键：requestDevice 需要用户手势，这里必须先同步完成清理，
  // 任何 await 都会消耗掉手势，导致设备选择框无法弹出。
  if (bleDevice != null) {
    try {
      bleDevice.removeEventListener('gattserverdisconnected', disconnect);
      if (bleDevice.gatt.connected) bleDevice.gatt.disconnect();
    } catch (e) {
      console.error(e);
    }
  }

  if (timerId != null) {
    clearInterval(timerId);
    timerId = null;
  }

  resetVariables();
  bleDevice = null;
  document.getElementById("btn-connect").innerHTML = '连接';
  updateButtonStatus();
  clearLog();
  addLog("已清除旧设备句柄，请在弹出的窗口中选择设备...");

  try {
    bleDevice = await navigator.bluetooth.requestDevice({
      optionalServices: ['62750001-d828-918d-fb46-b6c11c675aec'],
      acceptAllDevices: true,
    });
  } catch (e) {
    console.error(e);
    const msg = e.message ? e.message : String(e);
    if (e.name === 'NotFoundError' || /cancel/i.test(msg)) {
      addLog("已取消设备选择。");
    } else {
      addLog("requestDevice: " + msg);
      addLog("无法选择设备，请确认蓝牙已开启，且浏览器支持 Web Bluetooth。");
    }
    return;
  }

  await bleDevice.addEventListener('gattserverdisconnected', disconnect);
  addLog("已选择设备: " + bleDevice.name + "，开始连接...");
  setTimeout(async function () { await connect(); }, 300);
}

async function reConnect() {
  if (bleDevice != null && bleDevice.gatt.connected)
    bleDevice.gatt.disconnect();
  resetVariables();
  addLog("正在重连");
  setTimeout(async function () { await connect(); }, 300);
}

function handleNotify(value, idx) {
  const data = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  const msg = textDecoder.decode(data);
  addLog(bytes2hex(data), '⇓');

  // 图像下发前设备会上报 "mtu=NNN rle=1"
  if (msg.startsWith('mtu=')) {
    const n = parseInt(msg.substring(4), 10);
    if (!isNaN(n) && n > 20) EpdLink.mtu = n - 2;
    EpdLink.rle = /rle=1/.test(msg);
    EpdLink.reported = true;
    addLog(`设备 MTU=${isNaN(n) ? '?' : n}，单块载荷 ${EpdLink.mtu}B` + (EpdLink.rle ? '，支持 RLE' : ''));
    return;
  }

  if (idx == 0) {
    if (data.length > 12) currentRotate = data[12];
    return;
  }

  if (msg.startsWith('t=') && msg.length > 2) {
    const t = parseInt(msg.substring(2)) + new Date().getTimezoneOffset() * 60;
    addLog(`远端时间: ${new Date(t * 1000).toLocaleString()}`);
    addLog(`本地时间: ${new Date().toLocaleString()}`);
  }
}

function timeoutHandler() {
  if (gattServer == null || !gattServer.connected) {
    clearInterval(timerId);
    timerId = null;
    return;
  }

  const now = new Date().getTime();
  if (now - lastWriteTime > timeoutSeconds * 1000) {
    addLog(`${timeoutSeconds} 秒无操作，断开连接...`);
    if (bleDevice != null && bleDevice.gatt.connected) {
      bleDevice.gatt.disconnect();
    }
  }
}

async function connect(retryCount = 0) {
  if (bleDevice == null || epdCharacteristic != null) return;

  try {
    addLog("正在连接: " + bleDevice.name);
    gattServer = await bleDevice.gatt.connect();
    addLog('  找到 GATT Server');
    epdService = await gattServer.getPrimaryService('62750001-d828-918d-fb46-b6c11c675aec');
    addLog('  找到 EPD Service');
    epdCharacteristic = await epdService.getCharacteristic('62750002-d828-918d-fb46-b6c11c675aec');
    addLog('  找到 Characteristic');
  } catch (e) {
    console.error(e);
    const msg = e.message ? e.message : String(e);
    addLog("connect: " + msg);
    if (retryCount < MAX_CONNECT_RETRY) {
      addLog("连接失败，" + (CONNECT_RETRY_DELAY_MS / 1000) + " 秒后重试 (" + (retryCount + 1) + "/" + MAX_CONNECT_RETRY + ")...");
      await new Promise(r => setTimeout(r, CONNECT_RETRY_DELAY_MS));
      return connect(retryCount + 1);
    }
    addLog("连接多次失败，请排查：设备蓝牙是否已开启、是否已与本机配对、设备是否在附近、是否被其他手机/电脑占用、浏览器是否支持 Web Bluetooth。");
    disconnect();
    return;
  }

  try {
    await epdCharacteristic.startNotifications();
    epdCharacteristic.addEventListener('characteristicvaluechanged', (event) => {
      handleNotify(event.target.value, msgIndex++);
    });
  } catch (e) {
    console.error(e);
    if (e.message) addLog("startNotifications: " + e.message);
    disconnect();
    return;
  }

  try {
    await write(EpdCmd.INIT);
  } catch (e) {
    console.error(e);
    if (e.message) addLog("init: " + e.message);
    disconnect();
    return;
  }

  document.getElementById("btn-connect").innerHTML = '断开';
  updateButtonStatus();
  timerId = setInterval(function () { timeoutHandler(); }, 1000);
}

function addLog(logTXT, action = '') {
  const log = document.getElementById("log");
  const now = new Date();
  const time = String(now.getHours()).padStart(2, '0') + ":" +
    String(now.getMinutes()).padStart(2, '0') + ":" +
    String(now.getSeconds()).padStart(2, '0') + " ";

  const logEntry = document.createElement('div');
  const timeSpan = document.createElement('span');
  timeSpan.className = 'time';
  timeSpan.textContent = time;
  logEntry.appendChild(timeSpan);

  if (action !== '') {
    const actionSpan = document.createElement('span');
    actionSpan.className = 'action';
    actionSpan.innerHTML = action;
    logEntry.appendChild(actionSpan);
  }
  logEntry.appendChild(document.createTextNode(logTXT));

  log.appendChild(logEntry);
  log.scrollTop = log.scrollHeight;

  while (log.childNodes.length > 20) {
    log.removeChild(log.firstChild);
  }
}

function clearLog() {
  document.getElementById("log").innerHTML = '';
}

// ---------- 每天自动蓝牙更新 ----------
let autoUpdateEnabled = false;
let autoUpdateTime = "08:00"; // HH:MM
let autoCheckTimerId = null;
let lastAutoUpdateDate = ""; // YYYY-MM-DD，防止同天重复触发

function toggleAutoUpdate() {
  const chk = document.getElementById("chk-auto-update");
  autoUpdateEnabled = chk.checked;
  const status = document.getElementById("auto-status");
  if (autoUpdateEnabled) {
    status.textContent = "自动更新：已启用（每日 " + autoUpdateTime + "）";
    startAutoUpdateChecker();
    addLog("自动更新已启用，每日 " + autoUpdateTime + " 执行");
  } else {
    status.textContent = "自动更新：已停止";
    stopAutoUpdateChecker();
    addLog("自动更新已停止");
  }
}

function setAutoUpdateTime() {
  const inp = document.getElementById("time-auto-update");
  autoUpdateTime = inp.value;
  const status = document.getElementById("auto-status");
  status.textContent = "自动更新：" + (autoUpdateEnabled ? "已启用（每日 " + autoUpdateTime + "）" : "已停止");
  addLog("自动更新时间已设为 " + autoUpdateTime);
  if (autoUpdateEnabled) {
    startAutoUpdateChecker();
  }
}

function startAutoUpdateChecker() {
  stopAutoUpdateChecker();
  // 每分钟检查一次是否到达设定时间
  autoCheckTimerId = setInterval(() => {
    checkAutoUpdate();
  }, 60 * 1000);
  // 立即检查一次（页面刚打开可能正好在设定时间附近）
  checkAutoUpdate();
}

function stopAutoUpdateChecker() {
  if (autoCheckTimerId != null) {
    clearInterval(autoCheckTimerId);
    autoCheckTimerId = null;
  }
}

function checkAutoUpdate() {
  if (!autoUpdateEnabled) return;
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const curTime = hh + ":" + mm;
  const curDate = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");

  // 只在设定时间整点触发，且同一天只执行一次
  if (curTime === autoUpdateTime && curDate !== lastAutoUpdateDate) {
    lastAutoUpdateDate = curDate;
    addLog("到达每日自动更新时间 " + autoUpdateTime + "，开始自动蓝牙更新...");
    runAutoUpdate();
  }
}

async function runAutoUpdate() {
  const status = document.getElementById("auto-status");
  status.textContent = "自动更新：正在更新...";

  try {
    const connected = gattServer != null && gattServer.connected;

    if (!connected && bleDevice == null) {
      // 从未连接过，无法无手势自动弹出设备选择框
      addLog("自动更新：未找到已配对设备，请先手动连接一次");
      status.textContent = "自动更新：未配对设备，请先手动连接";
      return;
    }

    if (!connected) {
      // 已有设备但未连接，先重连
      addLog("自动更新：正在重连设备...");
      await reConnect();
    }

    // 连接成功后同步时间
    if (gattServer != null && gattServer.connected) {
      await syncTime(1);
      addLog("自动更新：每日蓝牙更新完成");
      status.textContent = "自动更新：已启用（每日 " + autoUpdateTime + "）";
    } else {
      addLog("自动更新：连接失败，已跳过本次更新");
      status.textContent = "自动更新：连接失败，已跳过";
    }
  } catch (e) {
    console.error(e);
    if (e.message) addLog("自动更新：失败 - " + e.message);
    status.textContent = "自动更新：执行出错";
  }
}

function testAutoUpdate() {
  addLog("手动触发自动更新测试...");
  lastAutoUpdateDate = ""; // 重置日期标记，允许立即执行
  runAutoUpdate();
}

document.body.onload = () => {
  updateButtonStatus();
  // 从 DOM 同步开关初始状态
  const chk = document.getElementById("chk-auto-update");
  if (chk) chk.checked = autoUpdateEnabled;
  const status = document.getElementById("auto-status");
  if (status) status.textContent = "自动更新：" + (autoUpdateEnabled ? "已启用（每日 " + autoUpdateTime + "）" : "已停止");
}