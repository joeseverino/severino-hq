package hostevents

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/godbus/dbus/v5"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// systemd's manager on the system bus (org.freedesktop.systemd1(5)).
const (
	systemdName    = "org.freedesktop.systemd1"
	systemdPath    = dbus.ObjectPath("/org/freedesktop/systemd1")
	systemdManager = systemdName + ".Manager"
	jobRemoved     = "JobRemoved"
)

// Systemd is the source of each job systemd ends as the contract's
// HostJobResult names, for any unit on the machine. systemd sends JobRemoved only to a client that
// asked with Subscribe, and stops when that client's connection closes.
func Systemd() Source {
	return func(ctx context.Context) (Stream, error) {
		conn, err := dbus.ConnectSystemBus(dbus.WithContext(ctx))
		if err != nil {
			return nil, err
		}
		// The bus delivers only what the manager itself sent, so no other
		// client of the bus can speak for it.
		if err := conn.AddMatchSignalContext(ctx,
			dbus.WithMatchSender(systemdName), dbus.WithMatchObjectPath(systemdPath),
			dbus.WithMatchInterface(systemdManager), dbus.WithMatchMember(jobRemoved),
		); err != nil {
			conn.Close()
			return nil, err
		}
		signals := make(chan *dbus.Signal, 16)
		conn.Signal(signals)
		if err := conn.Object(systemdName, systemdPath).CallWithContext(ctx, systemdManager+".Subscribe", 0).Err; err != nil {
			conn.Close()
			return nil, err
		}
		return jobs{signals, conn}, nil
	}
}

// errBusClosed is the bus connection ending, as it does when dbus restarts.
var errBusClosed = errors.New("the system bus closed")

type jobs struct {
	signals <-chan *dbus.Signal
	conn    interface{ Close() error }
}

func (j jobs) Close() error { return j.conn.Close() }

// Read hands each job end HQ is told of to heard, in order.
func (j jobs) Read(ctx context.Context, heard chan<- runtime.InboundArrival) error {
	for {
		var signal *dbus.Signal
		select {
		case <-ctx.Done():
			return ctx.Err()
		case received, open := <-j.signals:
			if !open {
				return errBusClosed
			}
			signal = received
		}
		if event, told := jobArrival(signal, time.Now()); told {
			if err := hand(ctx, heard, event); err != nil {
				return err
			}
		}
	}
}

// jobArrival is a JobRemoved(id, job, unit, result) as HQ is told it, the
// result after the event's dot, or false for any other signal or result. The
// signal carries no time, so its delivery id is systemd's job id, which no
// other job of this boot shares, and when it was heard.
func jobArrival(signal *dbus.Signal, heard time.Time) (runtime.InboundArrival, bool) {
	if signal.Path != systemdPath || signal.Name != systemdManager+"."+jobRemoved || len(signal.Body) != 4 {
		return runtime.InboundArrival{}, false
	}
	id, idOK := signal.Body[0].(uint32)
	unit, unitOK := signal.Body[2].(string)
	result, resultOK := signal.Body[3].(string)
	if !idOK || !unitOK || !resultOK || unit == "" || !runtime.HostJobResult(result).Valid() {
		return runtime.InboundArrival{}, false
	}
	heard = heard.UTC().Truncate(time.Microsecond)
	return runtime.InboundArrival{
		Provider:   runtime.InboundProviderHost,
		Event:      string(runtime.HostEventUnitJob) + "." + result,
		Subject:    unit,
		DeliveryID: fmt.Sprintf("job-%d-%d", id, heard.UnixNano()),
		ReceivedAt: heard,
	}, true
}
