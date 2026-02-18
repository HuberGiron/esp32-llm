#include <WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID = "Primavera26";
const char* WIFI_PASS = "Ib3r02026pR1m";

// Broker MQTT (TCP)
const char* MQTT_HOST = "test.mosquitto.org";
const int   MQTT_PORT = 1883;

// Tópicos
const char* TOPIC_CMD   = "huber/esp32/led/cmd";
const char* TOPIC_STATE = "huber/esp32/led/state";

// LED
const int LED_PIN = 2;

WiFiClient espClient;
PubSubClient mqtt(espClient);

// -------- Utilidades de debug ----------
static const char* mqttStateToStr(int s) {
  switch (s) {
    case MQTT_CONNECTION_TIMEOUT: return "MQTT_CONNECTION_TIMEOUT";
    case MQTT_CONNECTION_LOST:    return "MQTT_CONNECTION_LOST";
    case MQTT_CONNECT_FAILED:     return "MQTT_CONNECT_FAILED";
    case MQTT_DISCONNECTED:       return "MQTT_DISCONNECTED";
    case MQTT_CONNECTED:          return "MQTT_CONNECTED";
    case MQTT_CONNECT_BAD_PROTOCOL:return "MQTT_CONNECT_BAD_PROTOCOL";
    case MQTT_CONNECT_BAD_CLIENT_ID:return "MQTT_CONNECT_BAD_CLIENT_ID";
    case MQTT_CONNECT_UNAVAILABLE:return "MQTT_CONNECT_UNAVAILABLE";
    case MQTT_CONNECT_BAD_CREDENTIALS:return "MQTT_CONNECT_BAD_CREDENTIALS";
    case MQTT_CONNECT_UNAUTHORIZED:return "MQTT_CONNECT_UNAUTHORIZED";
    default: return "MQTT_STATE_UNKNOWN";
  }
}

void printWifiStatus() {
  Serial.print("[WiFi] Status=");
  Serial.print(WiFi.status());
  Serial.print("  IP=");
  Serial.print(WiFi.localIP());
  Serial.print("  GW=");
  Serial.print(WiFi.gatewayIP());
  Serial.print("  DNS=");
  Serial.print(WiFi.dnsIP());
  Serial.print("  RSSI=");
  Serial.print(WiFi.RSSI());
  Serial.println(" dBm");
}

// -------- MQTT callback ----------
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String msg;
  msg.reserve(length + 1);
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  msg.trim();

  Serial.print("[MQTT] RX topic=");
  Serial.print(topic);
  Serial.print(" payload='");
  Serial.print(msg);
  Serial.println("'");

  if (String(topic) == TOPIC_CMD) {
    if (msg.equalsIgnoreCase("ON")) {
      digitalWrite(LED_PIN, HIGH);
      Serial.println("[LED] -> ON");
      mqtt.publish(TOPIC_STATE, "ON", true);
    } else if (msg.equalsIgnoreCase("OFF")) {
      digitalWrite(LED_PIN, LOW);
      Serial.println("[LED] -> OFF");
      mqtt.publish(TOPIC_STATE, "OFF", true);
    } else if (msg.equalsIgnoreCase("STATUS")) {
      const char* st = digitalRead(LED_PIN) ? "ON" : "OFF";
      Serial.print("[LED] STATUS -> ");
      Serial.println(st);
      mqtt.publish(TOPIC_STATE, st, true);
    } else {
      Serial.println("[MQTT] Comando desconocido (usa ON/OFF/STATUS)");
    }
  }
}

// -------- Conectar WiFi ----------
bool connectWifi(uint32_t timeoutMs = 15000) {
  Serial.println("\n=== PASO 1: WiFi connect ===");
  Serial.print("[WiFi] SSID: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // ayuda a estabilidad (opcional)
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t t0 = millis();
  wl_status_t last = (wl_status_t)255;

  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < timeoutMs) {
    wl_status_t s = WiFi.status();
    if (s != last) {
      last = s;
      Serial.print("[WiFi] status -> ");
      Serial.println((int)s);
    }
    delay(300);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[WiFi] Conectado ✅");
    printWifiStatus();
    return true;
  } else {
    Serial.println("[WiFi] Timeout ❌ (no conectó)");
    printWifiStatus();
    return false;
  }
}

// -------- Conectar MQTT ----------
bool connectMqtt(uint32_t timeoutMs = 10000) {
  Serial.println("\n=== PASO 2: MQTT connect ===");
  Serial.print("[MQTT] Host: ");
  Serial.print(MQTT_HOST);
  Serial.print(":");
  Serial.println(MQTT_PORT);

  // Info extra útil
  Serial.print("[MQTT] Resolviendo DNS... ");
  IPAddress ip;
  if (WiFi.hostByName(MQTT_HOST, ip)) {
    Serial.print("OK -> ");
    Serial.println(ip);
  } else {
    Serial.println("FALLO (DNS)");
  }

  String clientId = "esp32-led-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  Serial.print("[MQTT] clientId=");
  Serial.println(clientId);

  uint32_t t0 = millis();
  while (!mqtt.connected() && (millis() - t0) < timeoutMs) {
    Serial.print("[MQTT] Intentando conectar... ");
    bool ok = mqtt.connect(clientId.c_str());
    if (ok) {
      Serial.println("OK ✅");
      Serial.print("[MQTT] Subscribiendo a ");
      Serial.println(TOPIC_CMD);

      if (mqtt.subscribe(TOPIC_CMD)) {
        Serial.println("[MQTT] subscribe OK ✅");
      } else {
        Serial.println("[MQTT] subscribe FALLO ❌");
      }

      const char* st = digitalRead(LED_PIN) ? "ON" : "OFF";
      Serial.print("[MQTT] Publicando STATE retain -> ");
      Serial.println(st);
      mqtt.publish(TOPIC_STATE, st, true);

      return true;
    } else {
      int s = mqtt.state();
      Serial.print("FALLO ❌ state=");
      Serial.print(s);
      Serial.print(" (");
      Serial.print(mqttStateToStr(s));
      Serial.println(")");
      delay(1000);
    }
  }

  Serial.println("[MQTT] Timeout ❌ (no conectó)");
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);

  Serial.println("\n\n==============================");
  Serial.println("ESP32 MQTT LED - DEBUG BUILD");
  Serial.println("==============================");

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  Serial.print("[LED] PIN=");
  Serial.print(LED_PIN);
  Serial.println(" inicial=OFF");

  // WiFi
  if (!connectWifi()) {
    Serial.println("[BOOT] WiFi no conectó. Reintentará en loop().");
  }

  // MQTT setup
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);

  // MQTT connect
  if (WiFi.status() == WL_CONNECTED) {
    if (!connectMqtt()) {
      Serial.println("[BOOT] MQTT no conectó. Reintentará en loop().");
    }
  }
}

void loop() {
  static uint32_t lastPrint = 0;

  // Reintento WiFi
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[LOOP] WiFi desconectado -> reintentando...");
    connectWifi();
  }

  // Reintento MQTT
  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) {
    Serial.println("\n[LOOP] MQTT desconectado -> reintentando...");
    connectMqtt();
  }

  // Mantener MQTT
  mqtt.loop();

  // Heartbeat cada 5s para ver que no se congeló
  if (millis() - lastPrint > 5000) {
    lastPrint = millis();
    Serial.print("[HB] WiFi=");
    Serial.print(WiFi.status() == WL_CONNECTED ? "OK" : "NO");
    Serial.print(" | MQTT=");
    Serial.print(mqtt.connected() ? "OK" : "NO");
    Serial.print(" | LED=");
    Serial.println(digitalRead(LED_PIN) ? "ON" : "OFF");
  }
}
