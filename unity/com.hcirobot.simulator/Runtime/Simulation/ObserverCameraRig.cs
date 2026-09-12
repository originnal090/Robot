using UnityEngine;

namespace HciRobot.Simulator
{
    /// <summary>
    /// Smooth third-person chase camera for watching the virtual robot from behind.
    /// Keeps the robot, the ball and nearby obstacles in one Game view.
    /// </summary>
    [DisallowMultipleComponent]
    public sealed class ObserverCameraRig : MonoBehaviour
    {
        [SerializeField] private Transform target;
        [SerializeField] private Vector3 positionOffset = new Vector3(0f, 3.2f, -3.4f);
        [SerializeField] private Vector3 lookAtOffset = new Vector3(0f, 0.5f, 1.2f);
        [SerializeField, Range(1f, 20f)] private float followSmoothing = 6f;

        private Vector3 currentVelocity;

        public void Configure(Transform targetTransform)
        {
            target = targetTransform;
        }

        private void LateUpdate()
        {
            if (target == null)
            {
                return;
            }

            Vector3 desired = target.position + positionOffset;
            // The robot only rotates around Y; ignore heading so the chase view
            // stays world-stable and never spins with the robot.
            transform.position = Vector3.SmoothDamp(
                transform.position,
                desired,
                ref currentVelocity,
                1f / followSmoothing);
            transform.LookAt(target.position + lookAtOffset);
        }
    }
}
