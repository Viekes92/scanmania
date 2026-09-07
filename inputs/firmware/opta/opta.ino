/*
 * ScanMania — Arduino Opta Input Module
 * SM-NODE-TRIG | 172.16.0.100
 * Modbus TCP :502 | Web :80
 * I1=plate I2=CP1 I3=CP2 I4=stop
 *
 * Button: press N times quickly to toggle input N (1-4)
 * USER LED: solid = booted, blink = NUC offline
 * D0-D3: mirror input states
 */

#include <Ethernet.h>

byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x64 };
IPAddress ip(172, 16, 0, 100);
IPAddress gw(172, 16, 0, 1);
IPAddress sn(255, 255, 255, 0);

const int NUM_IN = 4;
const int IN_PIN[NUM_IN] = { A0, A1, A2, A3 };
const char* IN_NAME[NUM_IN] = { "START", "CP1", "CP2", "STOP" };
const int IN_LED[NUM_IN] = { LED_D0, LED_D1, LED_D2, LED_D3 };

bool state[NUM_IN] = {};
bool lastRaw[NUM_IN] = {};
unsigned long lastEdge[NUM_IN] = {};
unsigned long count[NUM_IN] = {};
unsigned long webOverride[NUM_IN] = {};

EthernetServer mbSrv(502);
EthernetServer webSrv(80);
EthernetClient mbClient;

unsigned long bootAt = 0;
unsigned long mbPolls = 0;
unsigned long lastMbAt = 0;

// Button press counter (for manual input triggering)
int btnPresses = 0;
unsigned long lastBtnPress = 0;
bool lastBtnState = false;
const unsigned long BTN_WINDOW_MS = 800;  // time window to count presses
const unsigned long BTN_DEBOUNCE_MS = 50;
unsigned long lastBtnEdge = 0;

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n[BOOT] ScanMania Opta starting...");

  for (int i = 0; i < NUM_IN; i++) {
    pinMode(IN_PIN[i], INPUT);
    pinMode(IN_LED[i], OUTPUT);
    digitalWrite(IN_LED[i], LOW);
  }

  // USER LED
  pinMode(LED_RESET, OUTPUT);
  digitalWrite(LED_RESET, LOW);  // off during boot

  // Button
  pinMode(BTN_USER, INPUT);

  Serial.println("[BOOT] Ethernet...");
  Ethernet.begin(mac, ip, gw, gw, sn);
  Serial.print("[BOOT] IP=");
  Serial.println(Ethernet.localIP());

  mbSrv.begin();
  webSrv.begin();

  bootAt = millis();
  digitalWrite(LED_RESET, HIGH);  // USER LED on = booted
  Serial.println("[BOOT] Ready! Modbus TCP :502, Web :80");
  Serial.println("[BOOT] Button: press 1-4 times to toggle inputs");
}

void loop() {
  // ---- Modbus TCP ----
  if (!mbClient || !mbClient.connected()) {
    EthernetClient newClient = mbSrv.available();
    if (newClient) {
      mbClient = newClient;
      Serial.println("[MB] Client connected");
    }
  }
  if (mbClient && mbClient.connected() && mbClient.available() >= 12) {
    handleModbus();
  }

  // ---- Physical button (press N times to toggle input N) ----
  handleButton();

  // ---- Read hardware inputs ----
  for (int i = 0; i < NUM_IN; i++) {
    if (webOverride[i] > 0 && (millis() - webOverride[i]) < 3000) continue;
    if (webOverride[i] > 0) {
      // Auto-release after 3s
      webOverride[i] = 0;
      state[i] = false;
      digitalWrite(IN_LED[i], LOW);
      Serial.print("[AUTO] ");
      Serial.print(IN_NAME[i]);
      Serial.println(" released");
    }
    bool raw = digitalRead(IN_PIN[i]) == HIGH;
    if (raw != lastRaw[i]) { lastEdge[i] = millis(); lastRaw[i] = raw; }
    if ((millis() - lastEdge[i]) >= 30 && raw != state[i]) {
      state[i] = raw;
      if (state[i]) count[i]++;
      digitalWrite(IN_LED[i], state[i] ? HIGH : LOW);
      Serial.print("[IN] ");
      Serial.print(IN_NAME[i]);
      Serial.println(state[i] ? " ON" : " OFF");
    }
  }

  // ---- Web server ----
  EthernetClient wc = webSrv.available();
  if (wc) {
    handleWeb(wc);
    delay(1);
    wc.stop();
  }

  // ---- LEDs ----
  for (int i = 0; i < NUM_IN; i++) {
    digitalWrite(IN_LED[i], state[i] ? HIGH : LOW);
  }

  // USER LED: solid = NUC connected, blink = offline
  bool nucOk = lastMbAt > 0 && (millis() - lastMbAt) < 2000;
  if (nucOk) {
    digitalWrite(LED_RESET, HIGH);
  } else if (millis() > 5000) {  // don't blink during first 5s boot
    digitalWrite(LED_RESET, (millis() % 1000 < 500) ? HIGH : LOW);
  }
}

// ---- Button handler: count presses in a window ----
void handleButton() {
  bool btnState = digitalRead(BTN_USER) == LOW;  // active LOW

  // Debounce
  if (btnState != lastBtnState) {
    if ((millis() - lastBtnEdge) >= BTN_DEBOUNCE_MS) {
      lastBtnEdge = millis();
      lastBtnState = btnState;

      // Count on press (not release)
      if (btnState) {
        btnPresses++;
        lastBtnPress = millis();
        Serial.print("[BTN] Press #");
        Serial.println(btnPresses);

        // Flash all D0-D3 briefly to acknowledge
        for (int i = 0; i < NUM_IN; i++) {
          digitalWrite(IN_LED[i], i < btnPresses ? HIGH : LOW);
        }
      }
    }
  }

  // After the window expires, trigger the corresponding input
  if (btnPresses > 0 && (millis() - lastBtnPress) > BTN_WINDOW_MS) {
    int idx = btnPresses - 1;  // 1 press = input 0, 4 presses = input 3
    if (idx >= 0 && idx < NUM_IN) {
      state[idx] = !state[idx];  // toggle
      if (state[idx]) count[idx]++;
      digitalWrite(IN_LED[idx], state[idx] ? HIGH : LOW);
      webOverride[idx] = millis();  // hold for 10s

      Serial.print("[BTN] Toggle ");
      Serial.print(IN_NAME[idx]);
      Serial.println(state[idx] ? " ON" : " OFF");
    }
    btnPresses = 0;

    // Restore LED states
    for (int i = 0; i < NUM_IN; i++) {
      digitalWrite(IN_LED[i], state[i] ? HIGH : LOW);
    }
  }
}

// ---- Modbus TCP handler ----
void handleModbus() {
  byte buf[32];
  int n = mbClient.read(buf, sizeof(buf));
  if (n < 12) return;

  uint16_t tid = (buf[0] << 8) | buf[1];
  byte func = buf[7];

  lastMbAt = millis();
  mbPolls++;

  if (func == 0x02) {
    uint16_t startAddr = (buf[8] << 8) | buf[9];
    uint16_t qty = (buf[10] << 8) | buf[11];
    if (startAddr >= NUM_IN) {
      // startAddr out of range — send Modbus exception 0x02 (Illegal Data Address)
      byte exc[9];
      exc[0] = buf[0]; exc[1] = buf[1];
      exc[2] = 0; exc[3] = 0;
      exc[4] = 0; exc[5] = 3;
      exc[6] = buf[6];
      exc[7] = func | 0x80;
      exc[8] = 0x02;
      mbClient.write(exc, 9);
      return;
    }
    if (startAddr + qty > NUM_IN) qty = NUM_IN - startAddr;
    if (qty == 0) {
      // Nothing to read — send Modbus exception 0x03 (Illegal Data Value)
      byte exc[9];
      exc[0] = buf[0]; exc[1] = buf[1];
      exc[2] = 0; exc[3] = 0;
      exc[4] = 0; exc[5] = 3;
      exc[6] = buf[6];
      exc[7] = func | 0x80;
      exc[8] = 0x03;
      mbClient.write(exc, 9);
      return;
    }

    byte byteCount = (qty + 7) / 8;
    byte inputData[1] = { 0 };
    for (int i = 0; i < qty && i < NUM_IN; i++) {
      if (state[startAddr + i]) {
        inputData[i / 8] |= (1 << (i % 8));
      }
    }

    byte resp[10];
    resp[0] = buf[0]; resp[1] = buf[1];
    resp[2] = 0; resp[3] = 0;
    uint16_t respLen = 3 + byteCount;
    resp[4] = respLen >> 8; resp[5] = respLen & 0xFF;
    resp[6] = buf[6];
    resp[7] = 0x02;
    resp[8] = byteCount;
    resp[9] = inputData[0];
    mbClient.write(resp, 10);

    if (mbPolls <= 3 || mbPolls % 200 == 0) {
      Serial.print("[MB] Poll #");
      Serial.print(mbPolls);
      Serial.print(" inputs=");
      for (int i = 0; i < NUM_IN; i++) Serial.print(state[i] ? "1" : "0");
      Serial.println();
    }
  } else {
    byte resp[9];
    resp[0] = buf[0]; resp[1] = buf[1];
    resp[2] = 0; resp[3] = 0;
    resp[4] = 0; resp[5] = 3;
    resp[6] = buf[6];
    resp[7] = func | 0x80;
    resp[8] = 0x01;
    mbClient.write(resp, 9);
  }
}

// ---- Web handler ----
void handleWeb(EthernetClient& wc) {
  // Read just the first line (GET /path HTTP/1.1) — don't wait for full headers
  char req[80] = {};
  int n = 0;
  unsigned long t0 = millis();

  // Wait up to 200ms for first byte
  while (!wc.available() && (millis() - t0) < 200) { /* spin */ }

  // Read first line only
  while (wc.available() && n < 79) {
    char c = wc.read();
    if (c == '\r' || c == '\n') break;
    req[n++] = c;
  }

  // Drain remaining headers (don't parse, just consume)
  t0 = millis();
  while (wc.connected() && (millis() - t0) < 100) {
    if (wc.available()) { wc.read(); t0 = millis(); }
  }

  Serial.print("[WEB] ");
  Serial.println(req);

  if (strstr(req, "/t?")) {
    char* ip2 = strstr(req, "i=");
    char* sp = strstr(req, "s=");
    if (ip2 && sp) {
      int idx = atoi(ip2+2), val = atoi(sp+2);
      if (idx >= 0 && idx < NUM_IN) {
        state[idx] = val;
        if (val) count[idx]++;
        digitalWrite(IN_LED[idx], val ? HIGH : LOW);
        webOverride[idx] = millis();
        Serial.print("[WEB] ");
        Serial.print(IN_NAME[idx]);
        Serial.println(val ? " ON" : " OFF");
      }
    }
    wc.println("HTTP/1.1 303 See Other\r\nLocation: /\r\nConnection: close\r\n");
    return;
  }

  unsigned long up = (millis() - bootAt) / 1000;
  bool nuc = lastMbAt > 0 && (millis() - lastMbAt) < 2000;

  wc.println("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nRefresh: 2\r\nConnection: close\r\n");
  wc.println("<html><head><title>SM-NODE-TRIG</title></head>");
  wc.println("<body style='font-family:monospace;margin:20px'>");
  wc.println("<h2>SM-NODE-TRIG</h2>");
  wc.print("<p>NUC: <b style='color:");
  wc.print(nuc ? "green'>CONNECTED" : "red'>OFFLINE");
  wc.print("</b> (");
  wc.print(mbPolls);
  wc.println(" polls)</p>");
  wc.print("<p style='color:gray'>Button: press 1-4x to toggle inputs</p><hr>");

  wc.println("<table border=1 cellpadding=8 cellspacing=0>");
  wc.println("<tr><th>#</th><th>Name</th><th>State</th><th>Count</th><th>Test</th></tr>");
  for (int i = 0; i < NUM_IN; i++) {
    wc.print("<tr><td>I");
    wc.print(i+1);
    wc.print("</td><td>");
    wc.print(IN_NAME[i]);
    wc.print("</td><td style='color:");
    wc.print(state[i] ? "green'>ON" : "gray'>OFF");
    wc.print("</td><td>");
    wc.print(count[i]);
    wc.print("</td><td><a href='/t?i=");
    wc.print(i);
    wc.print("&s=");
    wc.print(state[i] ? "0" : "1");
    wc.print("'>");
    wc.print(state[i] ? "RELEASE" : "TRIGGER");
    wc.println("</a></td></tr>");
  }
  wc.println("</table>");

  wc.print("<p style='color:gray;font-size:12px'>Uptime: ");
  wc.print(up);
  wc.println("s | Modbus TCP :502</p>");
  wc.println("</body></html>");
}
