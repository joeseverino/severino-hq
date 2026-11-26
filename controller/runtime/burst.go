package runtime

import (
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
)

// BurstWindow is how long every reader that tells HQ of events gathers a
// burst of them into one batch.
const BurstWindow = 100 * time.Millisecond

// InboundHeartbeat is how often a reader tells HQ it is listening while
// nothing happens: HQ's declaration, which holds a channel's silence to it.
var InboundHeartbeat = time.Duration(api.MustLimit("InboundHeartbeatSeconds", "default")) * time.Second

// Burst is what a reader tells HQ next: the events it heard close together,
// as one batch, or that it is still listening. The first event opens a
// window of Window, and the batch is due when the window ends or it holds
// MaxInboundEvents, the most HQ takes in one. With nothing heard, a batch
// with no events is due Heartbeat after the last was taken.
type Burst struct {
	Window, Heartbeat time.Duration
	events            []InboundArrival
	due               <-chan time.Time
}

// NewBurst is the pace every reader keeps.
func NewBurst() Burst { return Burst{Window: BurstWindow, Heartbeat: InboundHeartbeat} }

// Add holds event, and says whether the batch is full.
func (b *Burst) Add(event InboundArrival) (full bool) {
	b.events = append(b.events, event)
	if len(b.events) == 1 {
		b.due = time.After(b.Window)
	}
	return len(b.events) >= MaxInboundEvents
}

// Due fires when the batch is due.
func (b *Burst) Due() <-chan time.Time {
	if b.due == nil {
		b.due = time.After(b.Heartbeat)
	}
	return b.due
}

// Held is whether an event waits to be told.
func (b *Burst) Held() bool { return len(b.events) > 0 }

// Take is the batch due, which the burst then lets go of.
func (b *Burst) Take() []InboundArrival {
	events := b.events
	b.events, b.due = nil, nil
	return events
}
