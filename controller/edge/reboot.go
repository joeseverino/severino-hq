package edge

import (
	"bytes"
	"context"
	"fmt"
	"io"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
)

// reboot is whether this machine waits on a restart and what logind has
// scheduled: the document the launcher hands HQ's own controller, read the
// same way. A logind that does not answer fails the read, so nothing stands
// in for it.
func (r Receiver) reboot(ctx context.Context, stdout, stderr io.Writer) error {
	var scheduled bytes.Buffer
	command := r.process(ctx, r.Busctl, hostcore.ScheduledShutdown[1:]...)
	command.Stdout, command.Stderr = &scheduled, stderr
	if err := command.Run(); err != nil {
		return err
	}
	reading, err := hostcore.ReadReboot(r.RebootRequired, r.Born, scheduled.Bytes(), r.Now())
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(stdout, "%s\n", reading)
	return err
}
