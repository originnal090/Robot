using UnityEngine;
using UnityEngine.InputSystem;

public class FireFighterMovementAnimator : MonoBehaviour
{
    [SerializeField] private Animator animator;
    [SerializeField] private InputActionReference moveAction;
    [SerializeField] private float deadzone = 0.15f;
    [SerializeField] private float smoothSpeed = 8f;

    private float currentSpeed;
    private bool useExternalMotion;
    private float externalSpeed;

    private void Update()
    {
        float targetSpeed;
        if (useExternalMotion)
        {
            targetSpeed = externalSpeed;
        }
        else
        {
            Vector2 move = Vector2.zero;

            if (moveAction != null && moveAction.action != null)
                move = moveAction.action.ReadValue<Vector2>();

            targetSpeed = move.magnitude > deadzone ? move.magnitude : 0f;
        }

        currentSpeed = Mathf.MoveTowards(
            currentSpeed,
            targetSpeed,
            smoothSpeed * Time.deltaTime);

        animator.SetFloat("MoveSpeed", currentSpeed);
    }

    public void SetExternalMotion(bool enabled, float speed)
    {
        useExternalMotion = enabled;
        externalSpeed = Mathf.Clamp01(Mathf.Abs(speed));
    }
}
