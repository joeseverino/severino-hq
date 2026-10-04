package runtime

import "context"

type verificationKey struct{}

// WithVerification carries the claimed operation's verification policy to the
// action that applies it.
func WithVerification(ctx context.Context, policy *Verification) context.Context {
	if policy == nil {
		return ctx
	}
	return context.WithValue(ctx, verificationKey{}, *policy)
}

// VerificationFrom is the policy the operation declared, if any.
func VerificationFrom(ctx context.Context) (Verification, bool) {
	policy, ok := ctx.Value(verificationKey{}).(Verification)
	return policy, ok
}
