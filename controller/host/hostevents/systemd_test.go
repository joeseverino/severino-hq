package hostevents

import (
	"context"
	"strconv"
	"testing"

	"github.com/godbus/dbus/v5"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// jobRemovedSignal is systemd's JobRemoved(u id, o job, s unit, s result), as
// org.freedesktop.systemd1(5) declares it on the manager's object.
func jobRemovedSignal(id uint32, unit, result string) *dbus.Signal {
	return &dbus.Signal{
		Sender: ":1.0",
		Path:   "/org/freedesktop/systemd1",
		Name:   "org.freedesktop.systemd1.Manager.JobRemoved",
		Body:   []any{id, dbus.ObjectPath("/org/freedesktop/systemd1/job/" + strconv.FormatUint(uint64(id), 10)), unit, result},
	}
}

type closed struct{}

func (closed) Close() error { return nil }

// bus is a systemd source whose signals the test sends; each open of it
// arrives on opened, and closing a read's signals ends that read.
func bus(t *testing.T) (source Source, opened chan chan *dbus.Signal) {
	t.Helper()
	opened = make(chan chan *dbus.Signal, 8)
	return func(context.Context) (Stream, error) {
		signals := make(chan *dbus.Signal, 8)
		opened <- signals
		return jobs{signals, closed{}}, nil
	}, opened
}

// A job that ended as the contract's HostJobResult names is told at once, by
// its unit's name and the job's result; a job that ended any other way, or
// one of HQ's own passes, is not.
func TestAJobsEndIsToldAsTheUnitsJob(t *testing.T) {
	source, opened := bus(t)
	told, _ := watch(t, source)
	signals := arrived(t, opened)

	signals <- jobRemovedSignal(4018, host.ControllerUnit, "done")
	signals <- jobRemovedSignal(4019, host.GlanceUnit, "done")
	signals <- jobRemovedSignal(4020, "example-pull@0123abc.service", "done")
	signals <- jobRemovedSignal(4021, "example-pull@4567def.service", "failed")
	signals <- jobRemovedSignal(4022, "example-check.service", "skipped")

	events := arrivals(t, arrived(t, told))
	if len(events) != 2 {
		t.Fatalf("told %+v, want the done and the failed job", events)
	}
	for index, want := range []struct{ result, unit string }{
		{"done", "example-pull@0123abc.service"},
		{"failed", "example-pull@4567def.service"},
	} {
		got := events[index]
		if got.Provider != runtime.InboundProviderHost || got.Event != string(runtime.HostEventUnitJob)+"."+want.result || got.Subject != want.unit {
			t.Errorf("told %+v, want %s of %s", got, want.result, want.unit)
		}
	}
}

// One stream that ends ends the read of every stream, which begins again by
// telling HQ it reconnected, so HQ reads whatever either missed.
func TestABusThatClosesIsReadAgain(t *testing.T) {
	socket, asked := fakeEngine(t)
	source, opened := bus(t)
	_, reconnected := watch(t, docker(socket), source)

	arrived(t, asked)
	close(arrived(t, opened))
	arrived(t, reconnected)
	arrived(t, asked)
	arrived(t, opened)
	arrived(t, reconnected)
}
