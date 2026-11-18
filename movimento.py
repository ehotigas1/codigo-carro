from machine import Pin, SoftI2C
from time import ticks_ms, ticks_diff, sleep_ms
from math import atan2, sqrt, sin, cos, pi

# Montagem esperada: X aponta para frente, Y para esquerda, Z para cima.
# Se a placa estiver girada, ajuste a permutacao e os sinais dos eixos.
# Use uma transformacao que preserve um sistema de eixos destro.
EIXOS = (0, 1, 2)
SINAIS = (1, 1, 1)

class Movimento:
    def __init__(self):
        self.i2c = SoftI2C(sda=Pin(21), scl=Pin(22), freq=100000, timeout=10000)
        self.addr = None
        self.status = 'Sensor ausente'
        self.ultimo = ticks_ms()
        self.tentativa = ticks_ms() - 4000
        self.seq = 0
        self.bias = [0, 0, 0]
        self.ang = [0, 0, 0]
        self.linear = [0, 0, 0]
        self.giro = [0, 0, 0]
        self.soma = [0, 0, 0]
        self.n = 0

    def iniciar(self):
        self.addr = None
        for addr in (0x68, 0x69):
            try:
                if self.i2c.readfrom_mem(addr, 0x75, 1)[0] == 0x68:
                    self.addr = addr
                    break
            except OSError:
                pass
        if self.addr is None:
            raise OSError('MPU6050 nao detectado')
        # PLL X, filtro ~44 Hz, amostras 100 Hz, +/-250 graus/s, +/-2g.
        for reg, val in ((0x6B, 1), (0x1A, 3), (0x19, 9), (0x1B, 0), (0x1C, 0)):
            self.i2c.writeto_mem(self.addr, reg, bytes([val]))
        self.status = 'Calibrando: mantenha parado'
        self.n = 0
        self.soma = [0, 0, 0]
        self.ang = [0, 0, 0]
        self.linear = [0, 0, 0]
        self.ultimo = ticks_ms()

    def atualizar(self):
        agora = ticks_ms()
        if self.addr is None:
            if ticks_diff(agora, self.tentativa) < 3000:
                return
            self.tentativa = agora
            try:
                self.iniciar()
            except OSError:
                self.addr = None
                self.status = 'Sensor ausente: confira SDA22/SCL21'
            return
        decorrido = ticks_diff(agora, self.ultimo)
        if decorrido < 20:
            return
        self.ultimo = agora
        try:
            raw = self.i2c.readfrom_mem(self.addr, 0x3B, 14)
            valores = []
            for i in range(0, 14, 2):
                v = raw[i] * 256 + raw[i+1]
                valores.append(v - 65536 if v > 32767 else v)
            a0 = [v / 16384 for v in valores[:3]]
            g0 = [v / 131 for v in valores[4:]]
            a = [a0[EIXOS[i]] * SINAIS[i] for i in range(3)]
            g = [g0[EIXOS[i]] * SINAIS[i] for i in range(3)]
            roll = atan2(a[1], a[2])
            pitch = atan2(-a[0], sqrt(a[1]**2 + a[2]**2))
            if self.n < 100:
                # Reinicia a media se detectar movimento durante a calibracao.
                norma = sqrt(sum(v*v for v in a))
                if max(abs(v) for v in g) > 4 or abs(norma - 1) > 0.08:
                    self.n = 0
                    self.soma = [0, 0, 0]
                    self.status = 'Calibrando: mantenha parado'
                    return
                for i in range(3):
                    self.soma[i] += g[i]
                self.n += 1
                self.ang = [roll, pitch, 0]
                if self.n == 100:
                    self.bias = [v / 100 for v in self.soma]
                    self.status = 'Pronto'
                return
            self.giro = [g[i] - self.bias[i] for i in range(3)]
            # Pausas de rede longas nao viram grandes saltos de integracao.
            dt = min(decorrido / 1000, 0.1)
            fator = 0.5 / (0.5 + dt)
            self.ang[0] = fator * (self.ang[0] + self.giro[0]*pi/180*dt) + (1-fator)*roll
            self.ang[1] = fator * (self.ang[1] + self.giro[1]*pi/180*dt) + (1-fator)*pitch
            self.ang[2] = (self.ang[2] + self.giro[2]*pi/180*dt + pi) % (2*pi) - pi
            r, p = self.ang[:2]
            gravidade = [-sin(p), sin(r)*cos(p), cos(r)*cos(p)]
            for i in range(3):
                self.linear[i] = 0.65*self.linear[i] + 0.35*(a[i] - gravidade[i])
            self.seq += 1
            self.status = 'Pronto'
        except OSError:
            self.addr = None
            self.status = 'Sensor desconectado'

    def dados(self):
        valido = self.addr is not None and self.n == 100 and ticks_diff(ticks_ms(), self.ultimo) < 500
        return {'ok': valido, 'status': self.status, 'seq': self.seq,
                'ang': [round(v*180/pi, 1) for v in self.ang] if valido else [0, 0, 0],
                'acc': [round(v, 3) for v in self.linear] if valido else [0, 0, 0],
                'giro': [round(v, 1) for v in self.giro] if valido else [0, 0, 0]}
