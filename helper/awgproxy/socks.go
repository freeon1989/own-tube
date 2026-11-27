package main

import (
	"context"
	"crypto/subtle"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"strconv"
	"strings"
	"time"
)

// Socks — минимальный SOCKS5 (RFC 1928/1929): только CONNECT, только логин и пароль.
type Socks struct {
	User, Pass string
	Dial       func(ctx context.Context, host string, port uint16) (net.Conn, error)
}

func (s *Socks) Serve(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go s.handle(c)
	}
}

func readN(c net.Conn, n int) ([]byte, error) {
	b := make([]byte, n)
	_, err := io.ReadFull(c, b)
	return b, err
}

func (s *Socks) handle(c net.Conn) {
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(30 * time.Second))
	h, err := readN(c, 2)
	if err != nil || h[0] != 5 {
		return
	}
	methods, err := readN(c, int(h[1]))
	if err != nil {
		return
	}
	if !strings.ContainsRune(string(methods), 2) {
		_, _ = c.Write([]byte{5, 0xff})
		return
	}
	_, _ = c.Write([]byte{5, 2})
	v, err := readN(c, 2)
	if err != nil || v[0] != 1 {
		return
	}
	u, err := readN(c, int(v[1]))
	if err != nil {
		return
	}
	pl, err := readN(c, 1)
	if err != nil {
		return
	}
	p, err := readN(c, int(pl[0]))
	if err != nil {
		return
	}
	okU := subtle.ConstantTimeCompare(u, []byte(s.User)) == 1
	okP := subtle.ConstantTimeCompare(p, []byte(s.Pass)) == 1
	if !okU || !okP {
		_, _ = c.Write([]byte{1, 1})
		return
	}
	_, _ = c.Write([]byte{1, 0})
	req, err := readN(c, 4)
	if err != nil || req[0] != 5 {
		return
	}
	if req[1] != 1 {
		reply(c, 7)
		return
	}
	var host string
	switch req[3] {
	case 1:
		b, err := readN(c, 4)
		if err != nil {
			return
		}
		host = net.IP(b).String()
	case 4:
		b, err := readN(c, 16)
		if err != nil {
			return
		}
		host = net.IP(b).String()
	case 3:
		l, err := readN(c, 1)
		if err != nil {
			return
		}
		b, err := readN(c, int(l[0]))
		if err != nil {
			return
		}
		host = string(b)
	default:
		reply(c, 8)
		return
	}
	pb, err := readN(c, 2)
	if err != nil {
		return
	}
	port := binary.BigEndian.Uint16(pb)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	up, err := s.Dial(ctx, host, port)
	cancel()
	if err != nil {
		var de *dnsError
		switch {
		case errors.As(err, &de):
			reply(c, 4)
		case strings.Contains(err.Error(), "refused"):
			reply(c, 5)
		case strings.Contains(err.Error(), "timed out") || strings.Contains(err.Error(), "deadline"):
			reply(c, 4)
		default:
			reply(c, 1)
		}
		return
	}
	defer up.Close()
	reply(c, 0)
	_ = c.SetDeadline(time.Time{})
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, c); closeWrite(up); done <- struct{}{} }()
	go func() { _, _ = io.Copy(c, up); closeWrite(c); done <- struct{}{} }()
	<-done
	<-done
}

type writeCloser interface{ CloseWrite() error }

func closeWrite(c net.Conn) {
	if w, ok := c.(writeCloser); ok {
		_ = w.CloseWrite()
	}
}

func reply(c net.Conn, code byte) {
	_, _ = c.Write([]byte{5, code, 0, 1, 0, 0, 0, 0, 0, 0})
}

var _ = strconv.Itoa
