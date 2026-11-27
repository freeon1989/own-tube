// awgproxy: AmneziaWG-туннель только для own-tube, без системного адаптера и без прав администратора.
//
// Туннель живёт в памяти процесса (userspace netstack gVisor из amneziawg-go). Наружу процесс
// открывает только SOCKS5 на 127.0.0.1 со случайным портом и логином/паролем на один сеанс.
// Конфигурация приходит JSON-строкой в stdin (ключи не попадают в командную строку и в журнал).
// Дальше stdin — канал команд: "stats", "probe host:port". Закрытие stdin завершает процесс,
// поэтому туннель не переживает программу.
//
// Copyright (c) 2026 freeon1989. MIT.
package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/amnezia-vpn/amneziawg-go/v3/conn"
	"github.com/amnezia-vpn/amneziawg-go/v3/device"
	"owntube/awgproxy/netstack"
)

const version = "awgproxy 1.0 (amneziawg-go v3.1.20261116)"

type Peer struct {
	PublicKey    string   `json:"public_key"`
	PresharedKey string   `json:"preshared_key"`
	Endpoint     string   `json:"endpoint"`
	AllowedIPs   []string `json:"allowed_ips"`
	Keepalive    int      `json:"keepalive"`
}

type Config struct {
	PrivateKey string            `json:"private_key"`
	Addresses  []string          `json:"addresses"`
	DNS        []string          `json:"dns"`
	MTU        int               `json:"mtu"`
	Obf        map[string]string `json:"obf"` // jc, jmin, jmax, s1..s4, h1..h4, i1..i5
	Peers      []Peer            `json:"peers"`
	User       string            `json:"user"`
	Pass       string            `json:"pass"`
	ListenPort int               `json:"listen_port"` // только для тестового сервера
}

var obfKeys = map[string]bool{"jc": true, "jmin": true, "jmax": true, "s1": true, "s2": true, "s3": true, "s4": true,
	"h1": true, "h2": true, "h3": true, "h4": true, "i1": true, "i2": true, "i3": true, "i4": true, "i5": true}

func keyHex(b64 string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil || len(raw) != 32 {
		return "", errors.New("ключ не похож на ключ WireGuard (32 байта base64)")
	}
	return hex.EncodeToString(raw), nil
}

func resolveEndpoint(ep string) (string, error) {
	host, port, err := net.SplitHostPort(strings.TrimSpace(ep))
	if err != nil {
		return "", fmt.Errorf("адрес сервера: %w", err)
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		return netip.AddrPortFrom(ip, mustPort(port)).String(), nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(ips) == 0 {
		return "", errors.New("не удалось найти адрес сервера VPN по имени")
	}
	sort.SliceStable(ips, func(i, j int) bool { return ips[i].Unmap().Is4() && !ips[j].Unmap().Is4() })
	return netip.AddrPortFrom(ips[0].Unmap(), mustPort(port)).String(), nil
}

func mustPort(p string) uint16 {
	n, _ := strconv.Atoi(p)
	return uint16(n)
}

// uapi собирает текст настройки устройства. В журнал не выводится.
func uapi(c *Config, server bool) (string, error) {
	var b strings.Builder
	k, err := keyHex(c.PrivateKey)
	if err != nil {
		return "", err
	}
	fmt.Fprintf(&b, "private_key=%s\n", k)
	if c.ListenPort > 0 {
		fmt.Fprintf(&b, "listen_port=%d\n", c.ListenPort)
	}
	names := make([]string, 0, len(c.Obf))
	for n := range c.Obf {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		if !obfKeys[n] {
			continue
		}
		v := strings.TrimSpace(c.Obf[n])
		if v == "" || strings.ContainsAny(v, "\n\r") {
			continue
		}
		fmt.Fprintf(&b, "%s=%s\n", n, v)
	}
	b.WriteString("replace_peers=true\n")
	for _, p := range c.Peers {
		pk, err := keyHex(p.PublicKey)
		if err != nil {
			return "", err
		}
		fmt.Fprintf(&b, "public_key=%s\n", pk)
		if p.PresharedKey != "" {
			psk, err := keyHex(p.PresharedKey)
			if err != nil {
				return "", err
			}
			fmt.Fprintf(&b, "preshared_key=%s\n", psk)
		}
		if p.Endpoint != "" && !server {
			ep, err := resolveEndpoint(p.Endpoint)
			if err != nil {
				return "", err
			}
			fmt.Fprintf(&b, "endpoint=%s\n", ep)
		}
		if p.Keepalive > 0 {
			fmt.Fprintf(&b, "persistent_keepalive_interval=%d\n", p.Keepalive)
		}
		b.WriteString("replace_allowed_ips=true\n")
		for _, a := range p.AllowedIPs {
			a = strings.TrimSpace(a)
			if _, err := netip.ParsePrefix(a); err == nil {
				fmt.Fprintf(&b, "allowed_ip=%s\n", a)
			}
		}
	}
	return b.String(), nil
}

func parseAddrs(list []string) ([]netip.Addr, error) {
	var out []netip.Addr
	for _, a := range list {
		a = strings.TrimSpace(a)
		if a == "" {
			continue
		}
		if p, err := netip.ParsePrefix(a); err == nil {
			out = append(out, p.Addr())
			continue
		}
		ip, err := netip.ParseAddr(a)
		if err != nil {
			return nil, fmt.Errorf("адрес %q: %w", a, err)
		}
		out = append(out, ip)
	}
	return out, nil
}

type Tunnel struct {
	dev  *device.Device
	tnet *netstack.Net
	has4 bool
	has6 bool
	bind string

	dnsMu sync.Mutex
	dns   map[string]dnsEntry
}

// up поднимает туннель. На Windows сначала быстрый сокет RIO; если его нет (старые системы, Wine), обычный UDP.
func up(c *Config, server bool, logLevel int) (*Tunnel, error) {
	t, err := upWith(c, server, logLevel, conn.NewDefaultBind())
	if err != nil && runtime.GOOS == "windows" && strings.Contains(err.Error(), "wsaioctl") {
		t, err = upWith(c, server, logLevel, conn.NewStdNetBind())
		if t != nil {
			t.bind = "std"
		}
	}
	return t, err
}

func upWith(c *Config, server bool, logLevel int, bind conn.Bind) (*Tunnel, error) {
	addrs, err := parseAddrs(c.Addresses)
	if err != nil {
		return nil, err
	}
	if len(addrs) == 0 {
		return nil, errors.New("в конфигурации нет Address")
	}
	dns, err := parseAddrs(c.DNS)
	if err != nil {
		return nil, err
	}
	if len(dns) == 0 {
		dns = []netip.Addr{netip.MustParseAddr("1.1.1.1")}
	}
	mtu := c.MTU
	if mtu <= 0 {
		mtu = 1280
	}
	tdev, tnet, err := netstack.CreateNetTUN(addrs, dns, mtu)
	if err != nil {
		return nil, err
	}
	dev := device.NewDevice(tdev, bind, device.NewLogger(logLevel, "awg: "))
	text, err := uapi(c, server)
	if err != nil {
		dev.Close()
		return nil, err
	}
	if err := dev.IpcSet(text); err != nil {
		dev.Close()
		return nil, fmt.Errorf("настройка AmneziaWG: %v", err)
	}
	if err := dev.Up(); err != nil {
		dev.Close()
		return nil, err
	}
	t := &Tunnel{dev: dev, tnet: tnet, bind: "default"}
	for _, a := range addrs {
		if a.Is4() {
			t.has4 = true
		} else {
			t.has6 = true
		}
	}
	return t, nil
}

type Stats struct {
	HandshakeAgeSec int64  `json:"handshake_age_sec"` // -1: рукопожатия ещё не было
	Rx              uint64 `json:"rx"`
	Tx              uint64 `json:"tx"`
}

func (t *Tunnel) stats() Stats {
	s := Stats{HandshakeAgeSec: -1}
	text, err := t.dev.IpcGet()
	if err != nil {
		return s
	}
	var hs int64
	for _, line := range strings.Split(text, "\n") {
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch k {
		case "last_handshake_time_sec":
			n, _ := strconv.ParseInt(v, 10, 64)
			if n > hs {
				hs = n
			}
		case "rx_bytes":
			n, _ := strconv.ParseUint(v, 10, 64)
			s.Rx += n
		case "tx_bytes":
			n, _ := strconv.ParseUint(v, 10, 64)
			s.Tx += n
		}
	}
	if hs > 0 {
		s.HandshakeAgeSec = time.Now().Unix() - hs
	}
	return s
}

// dial: имя хоста разрешается DNS-сервером из конфигурации через сам туннель.
func (t *Tunnel) dial(ctx context.Context, host string, port uint16) (net.Conn, error) {
	ip, err := netip.ParseAddr(host)
	if err != nil {
		ips, lerr := t.lookup(ctx, host)
		if lerr != nil || len(ips) == 0 {
			return nil, &dnsError{host}
		}
		var pick []netip.Addr
		for _, s := range ips {
			a, perr := netip.ParseAddr(s)
			if perr != nil {
				continue
			}
			a = a.Unmap()
			if (a.Is4() && t.has4) || (a.Is6() && t.has6) {
				pick = append(pick, a)
			}
		}
		if len(pick) == 0 {
			return nil, &dnsError{host}
		}
		sort.SliceStable(pick, func(i, j int) bool { return pick[i].Is4() && !pick[j].Is4() })
		var last error
		for _, a := range pick {
			c, derr := t.tnet.DialContextTCPAddrPort(ctx, netip.AddrPortFrom(a, port))
			if derr == nil {
				return c, nil
			}
			last = derr
		}
		return nil, last
	}
	return t.tnet.DialContextTCPAddrPort(ctx, netip.AddrPortFrom(ip.Unmap(), port))
}

// lookup: DNS через туннель с кэшем на 5 минут — каждое новое соединение не тратит лишний RTT на запрос имени.
func (t *Tunnel) lookup(ctx context.Context, host string) ([]string, error) {
	t.dnsMu.Lock()
	if e, ok := t.dns[host]; ok && time.Since(e.at) < 5*time.Minute {
		t.dnsMu.Unlock()
		return e.ips, nil
	}
	t.dnsMu.Unlock()
	ips, err := t.tnet.LookupContextHost(ctx, host)
	if err == nil && len(ips) > 0 {
		t.dnsMu.Lock()
		if t.dns == nil || len(t.dns) > 2048 {
			t.dns = map[string]dnsEntry{}
		}
		t.dns[host] = dnsEntry{ips, time.Now()}
		t.dnsMu.Unlock()
	}
	return ips, err
}

type dnsEntry struct {
	ips []string
	at  time.Time
}

type dnsError struct{ host string }

func (e *dnsError) Error() string { return "DNS через туннель не нашёл адрес" }

func emit(w *json.Encoder, mu *sync.Mutex, v any) {
	mu.Lock()
	defer mu.Unlock()
	_ = w.Encode(v)
}

func run() int {
	in := bufio.NewReaderSize(os.Stdin, 1<<16)
	line, err := in.ReadString('\n')
	if err != nil && line == "" {
		fmt.Fprintln(os.Stderr, "нет конфигурации в stdin")
		return 2
	}
	var c Config
	if err := json.Unmarshal([]byte(line), &c); err != nil {
		fmt.Fprintln(os.Stderr, "конфигурация не разобрана")
		return 2
	}
	out := json.NewEncoder(os.Stdout)
	var mu sync.Mutex
	if c.User == "" || c.Pass == "" {
		emit(out, &mu, map[string]any{"ready": false, "error": "нет токена сеанса"})
		return 2
	}
	t, err := up(&c, false, device.LogLevelError)
	if err != nil {
		emit(out, &mu, map[string]any{"ready": false, "error": err.Error()})
		return 1
	}
	defer t.dev.Close()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		emit(out, &mu, map[string]any{"ready": false, "error": err.Error()})
		return 1
	}
	srv := &Socks{User: c.User, Pass: c.Pass, Dial: t.dial}
	go srv.Serve(ln)
	emit(out, &mu, map[string]any{"ready": true, "port": ln.Addr().(*net.TCPAddr).Port, "version": version, "bind": t.bind})
	for {
		cmd, err := in.ReadString('\n')
		cmd = strings.TrimSpace(cmd)
		if cmd != "" {
			go handleCmd(t, cmd, out, &mu)
		}
		if err != nil {
			return 0 // stdin закрыт: программа завершилась — туннель тоже
		}
	}
}

func handleCmd(t *Tunnel, cmd string, out *json.Encoder, mu *sync.Mutex) {
	switch {
	case cmd == "stats":
		emit(out, mu, map[string]any{"stats": t.stats()})
	case strings.HasPrefix(cmd, "probe "):
		target := strings.TrimSpace(strings.TrimPrefix(cmd, "probe "))
		host, port, err := net.SplitHostPort(target)
		res := map[string]any{"target": target}
		if err == nil {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			t0 := time.Now()
			c, derr := t.dial(ctx, host, mustPort(port))
			cancel()
			if derr != nil {
				res["error"] = derr.Error()
			} else {
				res["ms"] = time.Since(t0).Milliseconds()
				c.Close()
			}
		} else {
			res["error"] = "адрес"
		}
		emit(out, mu, map[string]any{"probe": res})
	}
}

func main() {
	ver := flag.Bool("version", false, "версия")
	self := flag.Bool("selftest", false, "проверка туннеля в одном процессе (клиент и сервер через 127.0.0.1)")
	bench := flag.Int("bench-mb", 0, "selftest: сколько МБ прокачать для замера скорости")
	testServer := flag.Bool("test-server", false, "тестовый сервер AmneziaWG (только для разработки)")
	genkey := flag.Bool("test-keypair", false, "пара ключей для тестового стенда (JSON)")
	flag.Parse()
	switch {
	case *ver:
		fmt.Println(version)
	case *genkey:
		priv, pub := keypair()
		b, _ := json.Marshal(map[string]string{"private": priv, "public": pub})
		fmt.Println(string(b))
	case *self:
		os.Exit(selftest(*bench))
	case *testServer:
		os.Exit(runTestServer())
	default:
		os.Exit(run())
	}
}

var _ = io.EOF
