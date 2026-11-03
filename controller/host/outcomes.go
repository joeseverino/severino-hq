package host

import "errors"

// The outcomes a production deploy can end in are declared once, in
// deploy/outcomes.json: hq-host exits with an outcome's status, the Deploy
// workflow reads the same file to report it on the deployment, and HQ's
// reading of a deployment tells them apart by it.
//go:generate yq --input-format json --output-format yaml --split-exp "\"outcomes.gen.go\"" --from-file outcomes.yq ../../deploy/outcomes.json

// NeverExited is the Exit of an outcome hq-host never answered: a deploy not
// approved or superseded before it began, or one interrupted after it began.
const NeverExited = -1

// DeployOutcome is one way a deploy ends.
type DeployOutcome struct {
	Name string
	// Exit is the status hq-host deploy exits with.
	Exit int
	// State and Description are the deployment status the workflow posts.
	State, Description string
	// Production is what production runs after it: release, previous or unknown.
	Production string
	// CheckFailed is whether a check the deploy ran failed, whatever runs.
	CheckFailed bool
}

// ReportedOutcome is the outcome a deployment status reports, found by its
// state and description together, so a status the workflow did not write
// from the declaration (another repository's, or one before a rewording)
// matches none.
func ReportedOutcome(state, description string) (DeployOutcome, bool) {
	for _, outcome := range DeployOutcomes {
		if outcome.State == state && outcome.Description == description {
			return outcome, true
		}
	}
	return DeployOutcome{}, false
}

// outcomeError is a deploy's failure that ended in an outcome other than
// not-deployed. It says so the way net.Error says Timeout, through a method a
// caller asks for, however the error is wrapped.
type outcomeError struct {
	error
	outcome DeployOutcome
}

func (e outcomeError) Outcome() DeployOutcome { return e.outcome }

func (e outcomeError) Unwrap() error { return e.error }

// OutcomeOf is the outcome a deploy's error reports: live for none, the one an
// error carries, and not-deployed for any other, since every other failure
// of a deploy leaves the previous image running.
func OutcomeOf(err error) DeployOutcome {
	if err == nil {
		return DeployLive
	}
	var carried interface{ Outcome() DeployOutcome }
	if errors.As(err, &carried) {
		return carried.Outcome()
	}
	return DeployNotDeployed
}
