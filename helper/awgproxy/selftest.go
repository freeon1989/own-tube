package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"strconv"
	"time"

	"github.com/amnezia-vpn/amneziawg-go/v3/device"
	"golang.org/x/crypto/curve25519"
	"golang.org/x/net/proxy"
)

// Тестовые параметры обфускации в духе AmneziaWG 1.5/2.0. Только для проверки, не для настоящих серверов.
var testObf = map[string]string{
	"jc": "4", "jmin": "40", "jmax": "70", "s1": "15", "s2": "18", "s3": "20", "s4": "8",
	"h1": "1000000-1000100", "h2": "2000000-2000100", "h3": "3000000-3000100", "h4": "4000000-4000100",
	"i1": "<b 0xc70000000108><r 16><t>",
}

const (
	srvIP  = "10.77.0.1"
	cliIP  = "10.77.0.2"
	dnsTag = "test.owntube"
)

func keypair() (string, string) {
	var priv [32]byte
	_, _ = rand.Read(priv[:])
	priv[0] &= 248
	priv[31] = (priv[31] & 127) | 64
	pub, _ := curve25519.X25519(priv[:], curve25519.Basepoint)
	return base64.StdEncoding.EncodeToString(priv[:]), base64.StdEncoding.EncodeToString(pub)
}

func freeUDPPort() int {
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		return 51820
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).Port
}

// testServer поднимает сервер в netstack: HTTP на 10.77.0.1:80 и DNS на 10.77.0.1:53 (test.owntube → 10.77.0.1).
func testServer(port int, cliPub string) (*Tunnel, string, error) {
	sPriv, sPub := keypair()
	c := &Config{PrivateKey: sPriv, Addresses: []string{srvIP}, ListenPort: port, Obf: testObf, MTU: 1280,
		Peers: []Peer{{PublicKey: cliPub, AllowedIPs: []string{cliIP + "/32"}}}}
	t, err := up(c, true, device.LogLevelError)
	if err != nil {
		return nil, "", err
	}
	ln, err := t.tnet.ListenTCPAddrPort(netip.MustParseAddrPort(srvIP + ":80"))
	if err != nil {
		return nil, "", err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "hello through awg") })
	mux.HandleFunc("/bytes", func(w http.ResponseWriter, r *http.Request) {
		n, _ := strconv.Atoi(r.URL.Query().Get("n"))
		w.Header().Set("Content-Length", strconv.Itoa(n))
		buf := make([]byte, 64*1024)
		for n > 0 {
			k := min(n, len(buf))
			if _, err := w.Write(buf[:k]); err != nil {
				return
			}
			n -= k
		}
	})
	go func() { _ = http.Serve(ln, mux) }()
	udp, err := t.tnet.ListenUDPAddrPort(netip.MustParseAddrPort(srvIP + ":53"))
	if err != nil {
		return nil, "", err
	}
	go serveDNS(udp)
	return t, sPub, nil
}

// serveDNS отвечает A-записью 10.77.0.1 на любой вопрос о test.owntube, остальным — NXDOMAIN.
func serveDNS(c net.PacketConn) {
	buf := make([]byte, 1500)
	for {
		n, addr, err := c.ReadFrom(buf)
		if err != nil {
			return
		}
		q := buf[:n]
		if n < 17 {
			continue
		}
		// имя вопроса
		i, name := 12, ""
		for i < n && q[i] != 0 {
			l := int(q[i])
			if i+1+l > n {
				break
			}
			if name != "" {
				name += "."
			}
			name += string(q[i+1 : i+1+l])
			i += 1 + l
		}
		end := i + 5
		if end > n {
			continue
		}
		qtype := binary.BigEndian.Uint16(q[i+1 : i+3])
		resp := make([]byte, 0, 512)
		resp = append(resp, q[0], q[1], 0x81, 0x80, 0, 1, 0, 0, 0, 0, 0, 0)
		resp = append(resp, q[12:end]...)
		if name == dnsTag && qtype == 1 {
			resp[7] = 1
			resp = append(resp, 0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 10, 77, 0, 1)
		} else if name != dnsTag {
			resp[3] = 0x83
		}
		_, _ = c.WriteTo(resp, addr)
	}
}

func clientConfig(port int, cPriv, sPub string) *Config {
	return &Config{PrivateKey: cPriv, Addresses: []string{cliIP + "/32"}, DNS: []string{srvIP}, MTU: 1280, Obf: testObf,
		Peers: []Peer{{PublicKey: sPub, Endpoint: fmt.Sprintf("127.0.0.1:%d", port), AllowedIPs: []string{"0.0.0.0/0"}, Keepalive: 25}}}
}

func selftest(benchMB int) int {
	res := map[string]any{"version": version}
	defer func() { b, _ := json.Marshal(res); fmt.Println(string(b)) }()
	cPriv, cPub := keypair()
	port := freeUDPPort()
	st, sPub, err := testServer(port, cPub)
	if err != nil {
		res["error"] = "server: " + err.Error()
		return 1
	}
	defer st.dev.Close()
	cc := clientConfig(port, cPriv, sPub)
	cc.User, cc.Pass = "u", "p"
	ct, err := up(cc, false, device.LogLevelError)
	if err != nil {
		res["error"] = "client: " + err.Error()
		return 1
	}
	defer ct.dev.Close()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		res["error"] = err.Error()
		return 1
	}
	go (&Socks{User: "u", Pass: "p", Dial: ct.dial}).Serve(ln)
	d, _ := proxy.SOCKS5("tcp", ln.Addr().String(), &proxy.Auth{User: "u", Password: "p"}, proxy.Direct)
	cd := d.(proxy.ContextDialer)
	hc := &http.Client{Timeout: 60 * time.Second, Transport: &http.Transport{DialContext: cd.DialContext}}
	t0 := time.Now()
	r, err := hc.Get("http://" + dnsTag + "/hello")
	if err != nil {
		res["error"] = "через туннель: " + err.Error()
		return 1
	}
	body, _ := io.ReadAll(r.Body)
	r.Body.Close()
	res["hello"] = string(body)
	res["first_ms"] = time.Since(t0).Milliseconds()
	res["dns_through_tunnel"] = string(body) == "hello through awg"
	// неверный пароль SOCKS отклоняется
	bad, _ := proxy.SOCKS5("tcp", ln.Addr().String(), &proxy.Auth{User: "u", Password: "x"}, proxy.Direct)
	if c, err := bad.Dial("tcp", srvIP+":80"); err == nil {
		c.Close()
		res["error"] = "SOCKS пустил с неверным паролем"
		return 1
	}
	res["bad_auth_rejected"] = true
	if benchMB > 0 {
		t1 := time.Now()
		r, err := hc.Get(fmt.Sprintf("http://%s/bytes?n=%d", srvIP, benchMB<<20))
		if err != nil {
			res["error"] = "замер: " + err.Error()
			return 1
		}
		n, _ := io.Copy(io.Discard, r.Body)
		r.Body.Close()
		sec := time.Since(t1).Seconds()
		res["bench_bytes"] = n
		res["bench_mbit_s"] = float64(n*8) / sec / 1e6
	}
	s := ct.stats()
	res["bind"] = ct.bind
	res["handshake_age_sec"] = s.HandshakeAgeSec
	res["rx"], res["tx"] = s.Rx, s.Tx
	res["ok"] = res["dns_through_tunnel"] == true && s.HandshakeAgeSec >= 0
	if res["ok"] != true {
		return 1
	}
	return 0
}

// runTestServer: сервер для тестов Python. Печатает JSON с клиентской конфигурацией (тестовые ключи) и ждёт сигнала.
func runTestServer() int {
	cPriv, cPub := keypair()
	port := freeUDPPort()
	if p, err := strconv.Atoi(os.Getenv("AWG_TEST_PORT")); err == nil && p > 0 {
		port = p
	}
	st, sPub, err := testServer(port, cPub)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer st.dev.Close()
	cc := clientConfig(port, cPriv, sPub)
	b, _ := json.Marshal(map[string]any{"client": cc, "port": port, "server_ip": srvIP, "dns_name": dnsTag})
	fmt.Println(string(b))
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt)
	go func() { _, _ = io.Copy(io.Discard, os.Stdin); ch <- os.Interrupt }()
	<-ch
	_ = context.Background
	return 0
}
