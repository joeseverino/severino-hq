package runtime

import "time"

// BurstWindow is how long every reader that tells HQ of events gathers a
// burst of them into one batch.
const BurstWindow = 100 * time.Millisecond

// Burst is the events a reader heard close together, told to HQ as one
// batch: the first opens a window of Window, and the batch is due when the
// window ends or it holds MaxInboundEvents, the most HQ takes in one.
type Burst struct {
	Window time.Duration
	events []InboundArrival
	due    <-chan time.Time
}

// Add holds event, and says whether the batch is full.
func (b *Burst) Add(event InboundArrival) (full bool) {
	b.events = append(b.events, event)
	if b.due == nil {
		b.due = time.After(b.Window)
	}
	return len(b.events) >= MaxInboundEvents
}

// Due fires when the window ends, and never while nothing is held.
func (b *Burst) Due() <-chan time.Time { return b.due }

// Held is whether an event waits to be told.
func (b *Burst) Held() bool { return len(b.events) > 0 }

// Take is the batch held, which the burst then lets go of.
func (b *Burst) Take() []InboundArrival {
	events := b.events
	b.events, b.due = nil, nil
	return events
}
