from machine import Pin, PWM, ADC
from time import ticks_ms, ticks_diff, sleep_ms
import network
import socket
import select
import json
import gc
from movimento import Movimento
from mqtt_carro import MQTTCarro
from machine import unique_id
import binascii
import os

# PREENCHA AQUI (hotspot do celular em 2,4 GHz)
WIFI_NOME = "RedmiNote8"
WIFI_SENHA = "batata123"

# HiveMQ publico: ESP usa TCP; navegador usa WebSocket seguro
USAR_MQTT = True
MQTT_IP = "broker.hivemq.com"
MQTT_PORTA = 1883
MQTT_WEBSOCKET = "wss://broker.hivemq.com:8884/mqtt"
MQTT_USUARIO = ""
MQTT_SENHA = ""
TOPICO_BASE = "eld050/carrinho01"  # Mesmo topico usado pelo HTML; exclusivo do grupo

# Ajuste somente se uma roda girar no sentido errado.
INVERTER_MOTOR_A = False
INVERTER_MOTOR_B = False
SERVO_INVERTIDO = True  # Transistor inversor do circuito da aula
CENTRO, DIREITA, ESQUERDA = 85, 40, 130
POTENCIA = 1023  # 100% de PWM
TEMPO_SEM_COMANDO = 800  # ms

led_wifi = Pin(2, Pin.OUT, value=0)
led_vm = Pin(33, Pin.OUT, value=0)
led_am = Pin(32, Pin.OUT, value=0)
led_vd = Pin(25, Pin.OUT, value=0)
adc = ADC(Pin(34))  # Mover o fio do divisor do GPIO13 para GPIO34
adc.atten(ADC.ATTN_11DB)

# ENA e ENB com jumpers. PWM nas entradas da ponte H.
in1 = PWM(Pin(15), freq=25000, duty=0)
in2 = PWM(Pin(19), freq=25000, duty=0)
in3 = PWM(Pin(18), freq=25000, duty=0)
in4 = PWM(Pin(5), freq=25000, duty=0)
# Anodo comum: 1023 apaga, 0 acende completamente.
r = PWM(Pin(14), freq=1000, duty=1023)
g = PWM(Pin(27), freq=1000, duty=1023)
b = PWM(Pin(26), freq=1000, duty=1023)
servo = PWM(Pin(23), freq=50)

def set_servo_angle(angle):
    if SERVO_INVERTIDO:
        duty = int((angle / 180) * (896 - 998) + 998)
    else:
        duty = round((500 + angle / 180 * 2000) * 1023 / 20000)
    servo.duty(duty)

def cor(vermelho, verde, azul):
    for pwm, valor in ((r, vermelho), (g, verde), (b, azul)):
        pwm.duty(1023 - round(valor * 1023 / 255))

def motor(pino1, pino2, sentido, inverter):
    # Desliga ambas as entradas antes de trocar o sentido.
    pino1.duty(0)
    pino2.duty(0)
    if inverter:
        sentido = -sentido
    if sentido > 0:
        pino1.duty(POTENCIA)
    elif sentido < 0:
        pino2.duty(POTENCIA)

estado = "parar"
ultimo_comando = ticks_ms()
tensao = 0
ultima_bateria = ticks_ms() - 500

def comandar(comando, velocidade=None):
    global estado, ultimo_comando, POTENCIA
    if comando not in ("frente", "tras", "esquerda", "direita", "re_esquerda", "re_direita", "parar"):
        return
    nova_potencia = POTENCIA
    if velocidade is not None:
        if not 0 <= velocidade <= 100:
            raise ValueError("Velocidade deve estar entre 0 e 100")
        nova_potencia = round(velocidade * 1023 / 100)
    mudou_potencia = nova_potencia != POTENCIA
    POTENCIA = nova_potencia
    ultimo_comando = ticks_ms()
    if comando == estado and comando != "parar" and not mudou_potencia:
        return
    sentido = 0 if comando == "parar" else (-1 if comando in ("tras", "re_esquerda", "re_direita") else 1)
    # Pequena pausa na reversao frente/re; ambos os motores desligados.
    if (estado in ("tras", "re_esquerda", "re_direita") and sentido == 1) or (estado in ("frente", "direita", "esquerda") and sentido == -1):
        for p in (in1, in2, in3, in4):
            p.duty(0)
        sleep_ms(40)
    angulo = ESQUERDA if comando in ("esquerda", "re_esquerda") else DIREITA if comando in ("direita", "re_direita") else CENTRO
    set_servo_angle(angulo)
    motor(in1, in2, sentido, INVERTER_MOTOR_A)
    motor(in3, in4, sentido, INVERTER_MOTOR_B)
    cores = {"frente": (160, 0, 255), "direita": (255, 80, 0),
             "esquerda": (0, 255, 255), "tras": (100, 255, 70),
             "re_esquerda": (0, 255, 255), "re_direita": (255, 80, 0),
             "parar": (0, 0, 0)}
    cor(*cores[comando])
    estado = comando

def atualizar_bateria():
    global ultima_bateria, tensao
    if ticks_diff(ticks_ms(), ultima_bateria) < 250:
        return
    ultima_bateria = ticks_ms()
    soma = 0
    for i in range(8):
        soma += adc.read_uv()
    # R1 = 100k entre bateria e ADC; R2 = 47k entre ADC e GND.
    tensao = (soma / 8 / 1000000) * 147 / 47
    led_vm.value(1)
    led_am.value(tensao >= 4)
    led_vd.value(tensao >= 6)

comandar("parar")
wifi = network.WLAN(network.STA_IF)
wifi.active(True)
servidor = None
movimento = Movimento()
identificador = binascii.hexlify(unique_id()).decode()
boot_id = binascii.hexlify(os.urandom(6)).decode()
ultima_publicacao = ticks_ms() - 300

def dados_status():
    return {"bateria": round(tensao, 2), "estado": estado,
            "velocidade": round(POTENCIA * 100 / 1023), "imu": movimento.dados(),
            "stamp": ticks_ms(), "token": boot_id + ":" + str(mqtt.sessao)}

def comando_mqtt(payload):
    try:
        d = json.loads(payload)
        if not isinstance(d, dict):
            return
        if d.get("token") != boot_id + ":" + str(mqtt.sessao):
            return
        stamp = d.get("stamp")
        if type(stamp) is not int or not 0 <= ticks_diff(ticks_ms(), stamp) <= 1000:
            return  # Comando atrasado: nunca retomar movimento antigo
        acao, valor = d.get("comando"), d.get("velocidade")
        if acao not in ("frente", "tras", "esquerda", "direita", "re_esquerda", "re_direita", "parar"):
            return
        if type(valor) is not int or not 0 <= valor <= 100:
            return
        if acao != "parar" and movimento.addr is not None and movimento.n < 100:
            comandar("parar")
            return
        comandar(acao, valor)
    except (ValueError, TypeError):
        return

mqtt = MQTTCarro(MQTT_IP, MQTT_PORTA, "esp32-" + identificador,
                 TOPICO_BASE + "/comando", comando_mqtt, MQTT_USUARIO, MQTT_SENHA)

def enviar_pagina(cliente):
    # Nao carregar o HTML inteiro nem juntar cabecalho e pagina na RAM.
    gc.collect()
    marcador = b'"__CARRO_CONFIG__"'
    config = json.dumps({"mqtt": USAR_MQTT, "url": MQTT_WEBSOCKET,
                         "base": TOPICO_BASE}).replace("<", "\\u003c").encode()
    cliente.settimeout(0.25)
    # Sem Content-Length: Connection: close delimita o corpo da resposta.
    cliente.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
    pendente = b""
    inicio_envio = ticks_ms()
    with open("controle.html", "rb") as arquivo:
        while True:
            if ticks_diff(ticks_ms(), inicio_envio) > 5000:
                raise OSError("Tempo de envio excedido")
            bloco = arquivo.read(512)
            if not bloco:
                if pendente:
                    cliente.sendall(pendente.replace(marcador, config))
                break
            pendente += bloco
            pos = pendente.find(marcador)
            while pos >= 0:
                if pos:
                    cliente.sendall(pendente[:pos])
                cliente.sendall(config)
                pendente = pendente[pos + len(marcador):]
                pos = pendente.find(marcador)
            # Guarda apenas a cauda para reconhecer marcadores entre blocos.
            limite = len(pendente) - len(marcador) + 1
            if limite > 0:
                cliente.sendall(pendente[:limite])
                pendente = pendente[limite:]
            atualizar_bateria()
            movimento.atualizar()
    gc.collect()

try:
    servidor = socket.socket()
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind(("0.0.0.0", 80))
    servidor.listen(2)
    eventos = select.poll()
    eventos.register(servidor, select.POLLIN)
    tentativa = ticks_ms() - 16000
    conectado_antes = False

    while True:
        atualizar_bateria()
        movimento.atualizar()
        if movimento.addr is not None and movimento.n < 100 and estado != "parar":
            comandar("parar")
        conectado = wifi.isconnected()
        led_wifi.value(conectado)
        if USAR_MQTT:
            mqtt.atualizar(conectado)
            if not mqtt.conectado and estado != "parar":
                comandar("parar")
            if mqtt.conectado and ticks_diff(ticks_ms(), ultima_publicacao) >= 200:
                if mqtt.publicar(TOPICO_BASE + "/estado", json.dumps(dados_status()).encode()):
                    ultima_publicacao = ticks_ms()
        if not conectado:
            if estado != "parar":
                comandar("parar")
            if ticks_diff(ticks_ms(), tentativa) >= 15000:
                tentativa = ticks_ms()
                try:
                    wifi.disconnect()
                    wifi.connect(WIFI_NOME, WIFI_SENHA)
                except OSError:
                    pass
                print("Conectando ao hotspot...")
            conectado_antes = False
            sleep_ms(20)
            continue
        if not conectado_antes:
            print("IP da ESP: " + wifi.ifconfig()[0])
            print("Abra o controle no navegador: http://" + wifi.ifconfig()[0])
            if USAR_MQTT:
                print("Comandos e telemetria via MQTT: " + MQTT_IP)
            conectado_antes = True
        if estado != "parar" and ticks_diff(ticks_ms(), ultimo_comando) > TEMPO_SEM_COMANDO:
            comandar("parar")
        if not eventos.poll(10):
            continue
        cliente = None
        try:
            cliente, endereco = servidor.accept()
            cliente.settimeout(0.08)
            pedido = b""
            inicio = ticks_ms()
            while b"\r\n\r\n" not in pedido:
                if len(pedido) > 2048 or ticks_diff(ticks_ms(), inicio) > 120:
                    raise OSError("Pedido incompleto")
                bloco = cliente.recv(512)
                if not bloco:
                    raise OSError("Conexao encerrada")
                pedido += bloco
            linha = pedido.split(b"\r\n", 1)[0].decode()
            metodo, caminho, versao = linha.split(" ")
            tipo = "application/json"
            codigo = "200 OK"
            if metodo == "POST" and caminho.startswith("/cmd/") and USAR_MQTT:
                codigo, corpo = "409 Conflict", b'{"erro":"Use a pagina inicial da ESP (MQTT pelo navegador)"}'
            elif metodo == "POST" and caminho.startswith("/cmd/"):
                partes = caminho[5:].split("?vel=")
                comando = partes[0]
                velocidade = None
                try:
                    if len(partes) != 2:
                        raise ValueError("Velocidade ausente")
                    velocidade = int(partes[1])
                    if not 0 <= velocidade <= 100:
                        raise ValueError("Velocidade invalida")
                except ValueError:
                    comando = "invalido"
                if comando not in ("frente", "tras", "esquerda", "direita", "re_esquerda", "re_direita", "parar"):
                    codigo, corpo = "400 Bad Request", b'{}'
                else:
                    if comando != "parar" and movimento.addr is not None and movimento.n < 100:
                        comandar("parar")
                        codigo, corpo = "409 Conflict", b'{"erro":"Calibrando sensor"}'
                    else:
                        comandar(comando, velocidade)
                        corpo = b'{"ok":true}' 
            elif metodo == "GET" and caminho == "/status":
                corpo = json.dumps(dados_status()).encode()
            elif metodo == "GET" and caminho == "/":
                # Abrir/recarregar o controle sempre para o carro.
                comandar("parar")
                enviar_pagina(cliente)
                continue
            else:
                codigo, corpo = "404 Not Found", b'{}'
            cabecalho = "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".format(codigo, tipo, len(corpo))
            cliente.sendall(cabecalho.encode())
            cliente.sendall(corpo)
        except (OSError, ValueError):
            pass
        finally:
            if cliente is not None:
                cliente.close()
except KeyboardInterrupt:
    pass
finally:
    comandar("parar")
    led_wifi.value(0)
    mqtt.fechar()
    if servidor is not None:
        servidor.close()
