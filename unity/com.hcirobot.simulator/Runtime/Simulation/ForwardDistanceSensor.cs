using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    public sealed class ForwardDistanceSensor : MonoBehaviour
    {
        public enum CastShape
        {
            Ray,
            Sphere
        }

        [SerializeField] private VirtualRobotTcpServer tcpServer;
        [SerializeField] private CastShape castShape = CastShape.Sphere;
        [SerializeField, Min(0.01f)] private float maximumDistanceMetres = 5f;
        [SerializeField, Min(0.001f)] private float sphereRadiusMetres = 0.04f;
        [SerializeField, Min(0.02f)] private float telemetryIntervalSeconds = 0.30f;
        [SerializeField] private LayerMask obstacleLayers = ~(1 << 2);
        [SerializeField] private QueryTriggerInteraction triggerInteraction = QueryTriggerInteraction.Ignore;
        [SerializeField] private int noHitMillimetres = 5000;
        [SerializeField] private bool drawDebugRay = true;
        [SerializeField] private bool ignoreParentColliders = true;

        private RaycastHit[] hitBuffer = new RaycastHit[16];

        private float nextSampleTime;

        public int LatestDistanceMillimetres { get; private set; } = 5000;
        public bool LatestSampleHit { get; private set; }
        public Vector3 LatestHitPoint { get; private set; }

        private void Reset()
        {
            tcpServer = GetComponentInParent<VirtualRobotTcpServer>();
        }

        private void OnEnable()
        {
            nextSampleTime = Time.unscaledTime;
        }

        private void Update()
        {
            if (Time.unscaledTime < nextSampleTime)
            {
                return;
            }

            nextSampleTime = Time.unscaledTime + Mathf.Max(0.02f, telemetryIntervalSeconds);
            SampleNow();
            if (tcpServer != null)
            {
                tcpServer.SendDistanceMillimetres(LatestDistanceMillimetres);
            }
        }

        public int SampleNow()
        {
            float maxDistance = Mathf.Max(0.01f, maximumDistanceMetres);
            int hitCount = castShape == CastShape.Sphere
                ? Physics.SphereCastNonAlloc(
                    transform.position,
                    Mathf.Max(0.001f, sphereRadiusMetres),
                    transform.forward,
                    hitBuffer,
                    maxDistance,
                    obstacleLayers,
                    triggerInteraction)
                : Physics.RaycastNonAlloc(
                    transform.position,
                    transform.forward,
                    hitBuffer,
                    maxDistance,
                    obstacleLayers,
                    triggerInteraction);

            bool didHit = false;
            RaycastHit nearest = default;
            for (int index = 0; index < hitCount; index++)
            {
                RaycastHit candidate = hitBuffer[index];
                if (candidate.collider == null || IsParentCollider(candidate.collider.transform))
                {
                    continue;
                }
                if (!didHit || candidate.distance < nearest.distance)
                {
                    nearest = candidate;
                    didHit = true;
                }
            }

            LatestSampleHit = didHit;
            LatestHitPoint = didHit ? nearest.point : transform.position + transform.forward * maxDistance;
            LatestDistanceMillimetres = didHit
                ? Mathf.Max(0, Mathf.RoundToInt(nearest.distance * 1000f))
                : Mathf.Max(0, noHitMillimetres);
            return LatestDistanceMillimetres;
        }

        private bool IsParentCollider(Transform candidate)
        {
            if (!ignoreParentColliders)
            {
                return false;
            }

            Transform root = transform.root;
            return candidate == root || candidate.IsChildOf(root);
        }

        private void OnDrawGizmosSelected()
        {
            if (!drawDebugRay)
            {
                return;
            }

            Gizmos.color = LatestSampleHit ? Color.red : Color.green;
            Vector3 end = Application.isPlaying
                ? LatestHitPoint
                : transform.position + transform.forward * Mathf.Max(0.01f, maximumDistanceMetres);
            Gizmos.DrawLine(transform.position, end);
            if (castShape == CastShape.Sphere)
            {
                Gizmos.DrawWireSphere(end, Mathf.Max(0.001f, sphereRadiusMetres));
            }
        }
    }
}
