#include <LiquidCrystal.h>
#include <SoftwareSerial.h>

LiquidCrystal lcd(12, 11, 5, 4, 3, 2);
SoftwareSerial BT(7, 6);

// System state
unsigned long lastBERTime   = 0;
unsigned long lastPacketTime = 0;
bool  rayleighMode    = false;
int   currentSNR      = 15;
float currentBER      = 0.0001;
String currentModulation = "16QAM";
String currentFEC        = "HAM(7,4)";
String receivedData      = "";

// Error tracking
int totalBits     = 0;
int errorBits     = 0;
int correctedBits = 0;

// Measured BER — EMA of per-packet post-FEC residual errors
float measuredBER      = 0.0001;
const float BER_ALPHA  = 0.3;

// Live display state
String rxStatus       = "ON";
String lastReceivedMsg = "";
String currentChannel  = "AWGN";
unsigned long lastLCDupdate = 0;

// Modulation thresholds
const float THRESH_BPSK_UP = 0.0180;
const float THRESH_BPSK_DN = 0.0120;
const float THRESH_QPSK_UP = 0.0060;
const float THRESH_QPSK_DN = 0.0030;

// Pad a String to exactly `width` chars (truncate or right-pad with spaces)
String lcdPad(String s, int width) {
  s = s.substring(0, min((int)s.length(), width));
  while ((int)s.length() < width) s += ' ';
  return s;
}

void updateLiveDisplay() {
  // Write every field in place — NO lcd.clear() so there is no flicker

  // Row 0: "RX STATUS:XXXXXXXXX"  (col 0-9 label, col 11-19 status, 9 chars)
  lcd.setCursor(0,  0); lcd.print("RX STATUS:");
  lcd.setCursor(11, 0); lcd.print(lcdPad(rxStatus, 9));

  // Row 1: "MSG:XXXXXXXXXXXXXXXX"  (col 0-3 label, col 4-19 message, 16 chars)
  lcd.setCursor(0, 1); lcd.print("MSG:");
  lcd.setCursor(4, 1); lcd.print(lcdPad(lastReceivedMsg, 16));

  // Row 2: "SNR:XX  BER:0.XXXXXX"  (col 4-7 snr, col 12-19 ber, 8 chars)
  lcd.setCursor(0, 2); lcd.print("SNR:");
  lcd.setCursor(4, 2); lcd.print(lcdPad(String(currentSNR), 4));
  lcd.setCursor(8, 2); lcd.print("BER:");
  lcd.setCursor(12, 2); lcd.print(currentBER, 6);   // always 8 chars: "0.XXXXXX"

  // Row 3: "CHNL:XXXXXXXXXXXXXXX"  (col 5-19 channel, 15 chars)
  lcd.setCursor(0, 3); lcd.print("CHNL:");
  lcd.setCursor(5, 3); lcd.print(lcdPad(currentChannel, 15));
}

void setup() {
  lcd.begin(20, 4);
  BT.begin(9600);
  randomSeed(analogRead(A0));

  lcd.clear();
  lcd.setCursor(4, 0); lcd.print("*WELCOME*");
  lcd.setCursor(2, 1); lcd.print("BER ADAPTIVE SYS");
  lcd.setCursor(5, 2); lcd.print("RX READY!!!");
  delay(3000);

  updateLiveDisplay();

  // Announce via BT — TX forwards to Python
  BT.println("RX_CONTROLLER:READY");
}

String decideModulation(float ber) {
  if (currentModulation == "BPSK") {
    if (ber < THRESH_BPSK_DN) return "QPSK";
    return "BPSK";
  } else if (currentModulation == "QPSK") {
    if (ber > THRESH_BPSK_UP) return "BPSK";
    if (ber < THRESH_QPSK_DN) return "16QAM";
    return "QPSK";
  } else {
    if (ber > THRESH_BPSK_UP) return "BPSK";
    if (ber > THRESH_QPSK_UP) return "QPSK";
    return "16QAM";
  }
}

String addBitErrors(String data, float ber) {
  String result = "";
  totalBits = data.length() * 8;
  errorBits = 0;
  for (int i = 0; i < data.length(); i++) {
    char errored = data[i];
    for (int bit = 0; bit < 8; bit++) {
      if ((random(0, 10000) / 10000.0) < ber) {
        errored ^= (1 << bit);
        errorBits++;
      }
    }
    result += errored;
  }
  return result;
}

String applyFEC(const String& errored, const String& original, const String& fecType,
                int& corrected, int& residual) {
  corrected = 0;
  residual  = 0;
  String result = "";

  int pCorrect;
  if      (fecType == "CONV")     pCorrect = 95;
  else if (fecType == "HAM(7,4)") pCorrect = 85;
  else if (fecType == "REP3X")    pCorrect = 70;
  else                            pCorrect = 0;

  for (int i = 0; i < errored.length(); i++) {
    char ec = errored[i];
    char oc = (i < (int)original.length()) ? original[i] : ec;
    char rc = ec;
    for (int bit = 0; bit < 8; bit++) {
      bool wasFlipped = (((ec >> bit) & 1) != ((oc >> bit) & 1));
      if (wasFlipped) {
        if (random(0, 100) < pCorrect) {
          if ((oc >> bit) & 1) rc |=  (1 << bit);
          else                  rc &= ~(1 << bit);
          corrected++;
        } else {
          residual++;
        }
      }
    }
    result += rc;
  }
  return result;
}

// Hex-encode binary data so \r/\n/| bytes never corrupt the line protocol
String toHex(const String& s) {
  String result = "";
  for (int i = 0; i < s.length(); i++) {
    char buf[3];
    sprintf(buf, "%02X", (unsigned char)s[i]);
    result += buf;
  }
  return result;
}

void loop() {

  // 1. RECEIVE FROM TX VIA BLUETOOTH (data packets + forwarded commands from Python)
  while (BT.available()) {
    String msg = BT.readStringUntil('\n');
    msg.trim();
    if (msg.length() == 0) continue;

    // Data packet detection: H:=HAM, R:=REP3X, V:=CONV, P:=NONE
    bool isDataPacket = (msg.length() > 2 && msg[1] == ':' &&
                         (msg[0]=='H' || msg[0]=='R' ||
                          msg[0]=='V' || msg[0]=='P'));

    if (isDataPacket) {
      lastPacketTime = millis();
      receivedData   = msg.substring(2);

      String erroredData = addBitErrors(receivedData, currentBER);
      int corrected = 0, residual = 0;
      String correctedData = applyFEC(erroredData, receivedData, currentFEC,
                                       corrected, residual);
      correctedBits = corrected;

      if (totalBits > 0) {
        float postFecBER = (float)residual / (float)totalBits;
        measuredBER = BER_ALPHA * postFecBER + (1.0 - BER_ALPHA) * measuredBER;
        if (measuredBER < 0.0001) measuredBER = 0.0001;
        if (measuredBER > 0.05)   measuredBER = 0.05;
      }

      lastReceivedMsg = receivedData;
      rxStatus        = "RECEIVED";
      updateLiveDisplay();

      // Send telemetry via BT — binary fields hex-encoded to prevent \r/\n corruption
      BT.print("RX_DATA:"); BT.print(toHex(erroredData));
      BT.print("|"); BT.print(totalBits);
      BT.print("|"); BT.print(errorBits);
      BT.print("|"); BT.print(correctedBits);
      BT.print("|"); BT.println(residual);

      BT.print("RX_CORRECTED:"); BT.println(toHex(correctedData));
      BT.println("RX_RAW:" + receivedData);

      // Hold RECEIVED status briefly, watching for back-to-back packets
      unsigned long startTime = millis();
      while ((millis() - startTime) < 2000) {
        if (BT.available()) { startTime = millis(); break; }
        delay(10);
      }
      rxStatus = "ON";
      updateLiveDisplay();

    } else if (msg.startsWith("SNR:")) {
      currentSNR = msg.substring(4).toInt();
      if (currentSNR < 1)  currentSNR = 1;
      if (currentSNR > 25) currentSNR = 25;
      updateLiveDisplay();
      BT.print("RX_SNR_UPDATED:"); BT.println(currentSNR);

    } else if (msg == "CH:RAY") {
      rayleighMode  = true;
      currentChannel = "RAYLEIGH";
      updateLiveDisplay();
      BT.println("RX_CH_UPDATED:RAYLEIGH");

    } else if (msg == "CH:AWG") {
      rayleighMode  = false;
      currentChannel = "AWGN";
      updateLiveDisplay();
      BT.println("RX_CH_UPDATED:AWGN");

    } else if (msg.startsWith("FEC:")) {
      currentFEC = msg.substring(4);
      currentFEC.trim();
      BT.println("FEC:" + currentFEC);
    }
  }

  // 2. CALCULATE BER FROM SNR (every 1 second)
  if (millis() - lastBERTime > 1000) {
    lastBERTime = millis();

    if (currentSNR <= 5)
      currentBER = 0.018 + (5  - currentSNR) * 0.0006;
    else if (currentSNR <= 15)
      currentBER = 0.007 + (15 - currentSNR) * 0.0009;
    else
      currentBER = 0.0001 + (25 - currentSNR) * 0.0004;

    // ±35% multiplicative jitter — models real estimation noise
    float jitter = 1.0 + (random(-350, 350) / 1000.0);
    currentBER *= jitter;

    if (rayleighMode && random(0, 100) < 15) {
      currentBER += random(500, 2000) / 10000.0;
      if (currentBER > 0.05) currentBER = 0.05;
    }

    if (currentBER < 0.0001) currentBER = 0.0001;
    if (currentBER > 0.05)   currentBER = 0.05;

    updateLiveDisplay();

    // BER telemetry → TX1 → Python
    BT.print("BER:"); BT.println(currentBER, 6);

    // Adaptation: prefer measured BER; fall back to SNR estimate if TX quiet >10 s
    float berForDecision = ((millis() - lastPacketTime) < 10000UL)
                           ? measuredBER : currentBER;
    String newMod = decideModulation(berForDecision);
    if (newMod != currentModulation) {
      currentModulation = newMod;

      // MOD_DECISION → TX1 forwards to Python dashboard
      BT.print("MOD_DECISION:"); BT.println(currentModulation);

      // CMD_MOD → TX1 handles locally to update its own display
      BT.print("CMD_MOD:"); BT.println(currentModulation);

      BT.println("RX:SENT_MOD_TO_TX");
    }
  }

  // 3. Periodic LCD refresh when idle
  if (millis() - lastLCDupdate > 500) {
    lastLCDupdate = millis();
    if (rxStatus == "ON") updateLiveDisplay();
  }
}
