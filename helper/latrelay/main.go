// latrelay: ретранслятор TCP/UDP с задержкой в каждую сторону — только для замеров (имитация далёкого сервера).
package main

import (
	"flag"
	"log"
	"net"
	"time"
)

type pkt struct {
	at   time.Time
	data []byte
}

func delayed(delay time.Duration, write func([]byte) error) chan<- pkt {
	ch := make(chan pkt, 65536)
	go func() {
		for p := range ch {
			if d := time.Until(p.at.Add(delay)); d > 0 {
				time.Sleep(d)
			}
			if write(p.data) != nil {
				for range ch {
				}
				return
			}
		}
	}()
	return ch
}

func pipeTCP(src, dst net.Conn, delay time.Duration) {
	out := delayed(delay, func(b []byte) error { _, err := dst.Write(b); return err })
	buf := make([]byte, 64*1024)
	for {
		n, err := src.Read(buf)
		if n > 0 {
			out <- pkt{time.Now(), append([]byte(nil), buf[:n]...)}
		}
		if err != nil {
			close(out)
			time.AfterFunc(delay+50*time.Millisecond, func() {
				if c, ok := dst.(*net.TCPConn); ok {
					_ = c.CloseWrite()
				}
			})
			return
		}
	}
}

func tcp(listen, target string, delay time.Duration) {
	ln, err := net.Listen("tcp", listen)
	if err != nil {
		log.Fatal(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go func(c net.Conn) {
			u, err := net.Dial("tcp", target)
			if err != nil {
				c.Close()
				return
			}
			go pipeTCP(c, u, delay)
			pipeTCP(u, c, delay)
		}(c)
	}
}

func udp(listen, target string, delay time.Duration) {
	la, _ := net.ResolveUDPAddr("udp", listen)
	ta, _ := net.ResolveUDPAddr("udp", target)
	front, err := net.ListenUDP("udp", la)
	if err != nil {
		log.Fatal(err)
	}
	back, err := net.DialUDP("udp", nil, ta)
	if err != nil {
		log.Fatal(err)
	}
	var client *net.UDPAddr
	toServer := delayed(delay, func(b []byte) error { _, err := back.Write(b); return err })
	toClient := delayed(delay, func(b []byte) error {
		if client != nil {
			_, _ = front.WriteToUDP(b, client)
		}
		return nil
	})
	go func() {
		buf := make([]byte, 65536)
		for {
			n, err := back.Read(buf)
			if err != nil {
				return
			}
			toClient <- pkt{time.Now(), append([]byte(nil), buf[:n]...)}
		}
	}()
	buf := make([]byte, 65536)
	for {
		n, a, err := front.ReadFromUDP(buf)
		if err != nil {
			return
		}
		client = a
		toServer <- pkt{time.Now(), append([]byte(nil), buf[:n]...)}
	}
}

func main() {
	proto := flag.String("proto", "tcp", "tcp|udp")
	listen := flag.String("listen", "127.0.0.1:0", "")
	target := flag.String("target", "", "")
	ms := flag.Int("delay-ms", 20, "задержка в каждую сторону")
	flag.Parse()
	d := time.Duration(*ms) * time.Millisecond
	if *proto == "udp" {
		udp(*listen, *target, d)
	} else {
		tcp(*listen, *target, d)
	}
}
