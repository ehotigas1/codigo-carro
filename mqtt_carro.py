"""Cliente MQTT 3.1.1 local, QoS 0, com leitura incremental e reconexao.
Sem dependencias externas. Nao implementa TLS; use na rede local da aula.
"""
import socket
import select
from time import ticks_ms, ticks_diff

def texto(valor):
    if isinstance(valor, str):
        valor = valor.encode()
    return bytes([len(valor) >> 8, len(valor) & 255]) + valor

def pacote(tipo, dados=b''):
    tamanho = len(dados)
    cab = bytearray([tipo])
    while True:
        b = tamanho % 128
        tamanho //= 128
        cab.append(b | (128 if tamanho else 0))
        if not tamanho:
            return bytes(cab) + dados

class MQTTCarro:
    def __init__(self, ip, porta, identificador, topico, callback, usuario='', senha=''):
        self.ip, self.porta, self.id = ip, porta, identificador
        self.topico, self.callback = topico, callback
        self.usuario, self.senha = usuario, senha
        self.sock = None
        self.fase = 0
        self.conectado = False
        self.rx = b''
        self.tx = b''
        self.tentativa = ticks_ms() - 5000
        self.ultima_resposta = ticks_ms()
        self.ultimo_ping = ticks_ms()
        self.sessao = 0

    def fechar(self):
        if self.sock:
            self.sock.close()
        self.sock = None
        self.conectado = False
        self.fase = 0
        self.rx = self.tx = b''
        self.tentativa = ticks_ms()

    def conectar(self):
        self.sock = socket.socket()
        # Resolucao somente na reconexao, quando o main mantem os motores parados.
        destino = socket.getaddrinfo(self.ip, self.porta, 0, socket.SOCK_STREAM)[0][-1]
        self.sock.settimeout(1.0)
        self.sock.connect(destino)
        self.sock.setblocking(False)
        flags = 2  # clean session: nao repetir comandos de uma sessao antiga
        corpo = texto(self.id)
        if self.usuario:
            flags |= 0xC0
            corpo += texto(self.usuario) + texto(self.senha)
        self.tx = pacote(0x10, texto('MQTT') + bytes([4, flags, 0, 10]) + corpo)
        self.fase = 1
        self.inicio = ticks_ms()
        self.poll = select.poll()
        self.poll.register(self.sock, select.POLLIN | select.POLLOUT)

    def publicar(self, topico, mensagem):
        # Descarta telemetria antiga se o socket ainda estiver ocupado.
        if not self.conectado or self.tx:
            return False
        self.tx = pacote(0x30, texto(topico) + mensagem)
        return True

    def tratar(self, tipo, dados):
        self.ultima_resposta = ticks_ms()
        if tipo == 0x20 and self.fase == 1:
            if dados != b'\x00\x00':
                raise OSError('Broker recusou conexao')
            self.tx += pacote(0x82, b'\x00\x01' + texto(self.topico) + b'\x00')
            self.fase = 2
        elif tipo == 0x90 and self.fase == 2:
            if dados != b'\x00\x01\x00':
                raise OSError('Broker recusou inscricao QoS0')
            self.fase = 3
            self.conectado = True
            self.sessao += 1
            self.ultimo_ping = ticks_ms()
        elif tipo >> 4 == 3 and self.conectado:
            # Nunca executar mensagens retidas nem comandos QoS1/2.
            if tipo & 7 or len(dados) < 2:
                return
            n = dados[0] * 256 + dados[1]
            if len(dados) < n + 2:
                raise OSError('Topico truncado')
            if dados[2:2+n] == self.topico.encode():
                self.callback(dados[2+n:])

    def receber(self):
        # Limites por ciclo preservam o tempo para bateria e sensor.
        for _ in range(4):
            if len(self.rx) < 2:
                return
            n, mult, pos = 0, 1, 1
            while True:
                if pos >= len(self.rx):
                    return
                b = self.rx[pos]
                n += (b & 127) * mult
                pos += 1
                if not b & 128:
                    break
                mult *= 128
                if pos > 4:
                    raise OSError('Pacote MQTT invalido')
            if n > 2048:
                raise OSError('Pacote muito grande')
            if len(self.rx) < pos+n:
                return
            tipo, dados = self.rx[0], self.rx[pos:pos+n]
            self.rx = self.rx[pos+n:]
            self.tratar(tipo, dados)

    def atualizar(self, wifi_ok):
        if not wifi_ok:
            if self.sock:
                self.fechar()
            return
        agora = ticks_ms()
        try:
            if self.sock is None:
                if ticks_diff(agora, self.tentativa) < 5000:
                    return
                self.tentativa = agora
                self.conectar()
                return
            if not self.conectado and ticks_diff(agora, self.inicio) > 2000:
                raise OSError('Timeout MQTT')
            if self.conectado:
                if ticks_diff(agora, self.ultima_resposta) > 12000:
                    raise OSError('Broker sem resposta')
                if not self.tx and ticks_diff(agora, self.ultimo_ping) > 4000:
                    self.tx = b'\xc0\x00'
                    self.ultimo_ping = agora
            self.poll.modify(self.sock, select.POLLIN | (select.POLLOUT if self.tx else 0))
            for _, flags in self.poll.poll(0):
                if flags & (select.POLLERR | select.POLLHUP):
                    raise OSError('Socket fechado')
                if flags & select.POLLOUT and self.tx:
                    try:
                        n = self.sock.send(self.tx)
                        if n:
                            self.tx = self.tx[n:]
                    except OSError as e:
                        if e.args[0] not in (11, 35, 10035, 116):
                            raise
                if flags & select.POLLIN:
                    bloco = self.sock.recv(512)
                    if not bloco:
                        raise OSError('Broker desconectado')
                    self.rx += bloco
                    if len(self.rx) > 4096:
                        raise OSError('Buffer MQTT cheio')
            self.receber()
        except OSError:
            self.fechar()
