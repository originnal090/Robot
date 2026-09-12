using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    public sealed class SimulationTrialRecorder : MonoBehaviour
    {
        [Header("Ground truth")]
        [SerializeField] private Transform robot;
        [SerializeField] private Transform target;
        [SerializeField] private Rigidbody robotBody;
        [SerializeField] private TonyPiMotionDriver motionDriver;
        [SerializeField] private ForwardDistanceSensor distanceSensor;
        [SerializeField] private AutonomyStatusUdpReceiver statusReceiver;
        [SerializeField] private string scenario = "quick-start";

        [Header("Pass thresholds")]
        [SerializeField, Min(0.01f)] private float maximumFinalDistanceMetres = 0.35f;
        [SerializeField, Range(0f, 180f)] private float maximumHeadingErrorDegrees = 15f;
        [SerializeField, Min(0.1f)] private float maximumDurationSeconds = 30f;
        [SerializeField, Min(0f)] private float requiredStableStopSeconds = 0.5f;
        [SerializeField, Min(0f)] private float stoppedLinearSpeed = 0.02f;
        [SerializeField, Min(0f)] private float stoppedAngularSpeed = 0.05f;

        [Header("Recording")]
        [SerializeField, Min(1f)] private float sampleRateHz = 10f;
        [SerializeField] private bool recordOnPlay = true;
        [SerializeField] private bool endWhenAutonomyFinishes = true;
        [SerializeField] private string outputDirectory = "HCIRobotTrials";
        [SerializeField] private string[] ignoredCollisionObjects = { "Ground", "Red Ball Target" };

        private readonly List<CollisionSample> pendingCollisions = new List<CollisionSample>();
        private StreamWriter writer;
        private string trialId;
        private string outputPath;
        private float startedRealtime;
        private float nextSampleRealtime;
        private float stableStopStarted = -1f;
        private int trajectorySamples;
        private int collisionCount;
        private int maximumAvoidCount;
        private float distanceTravelled;
        private float minimumClearanceMetres = float.PositiveInfinity;
        private Vector3 previousPosition;
        private string latestState;
        private string latestReason;
        private string latestTermination;
        private long latestStatusSequence = -1;
        private bool controllerArrived;
        private bool endingRequested;
        private float endingRequestedAt;

        public bool IsRecording => writer != null;
        public string OutputPath => outputPath;

        private void Reset()
        {
            robot = transform;
            robotBody = GetComponent<Rigidbody>();
            motionDriver = GetComponent<TonyPiMotionDriver>();
            distanceSensor = GetComponentInChildren<ForwardDistanceSensor>();
            statusReceiver = FindObjectOfType<AutonomyStatusUdpReceiver>();
        }

        private void OnEnable()
        {
            if (statusReceiver != null)
            {
                statusReceiver.StatusUpdated += OnStatusUpdated;
                statusReceiver.StatusTimedOut += OnStatusTimedOut;
            }
            if (recordOnPlay)
            {
                BeginTrial();
            }
        }

        private void Update()
        {
            if (!IsRecording)
            {
                return;
            }

            FlushCollisionEvents();
            UpdateStableStop();
            if (endingRequested &&
                (CurrentStableStopSeconds() >= requiredStableStopSeconds ||
                 Time.realtimeSinceStartup - endingRequestedAt >= Mathf.Max(1f, requiredStableStopSeconds + 1f)))
            {
                EndTrial(latestTermination);
                return;
            }
            if (Time.realtimeSinceStartup < nextSampleRealtime)
            {
                return;
            }

            nextSampleRealtime = Time.realtimeSinceStartup + 1f / Mathf.Max(1f, sampleRateHz);
            WriteTrajectorySample();
        }

        private void OnDisable()
        {
            if (statusReceiver != null)
            {
                statusReceiver.StatusUpdated -= OnStatusUpdated;
                statusReceiver.StatusTimedOut -= OnStatusTimedOut;
            }
            EndTrial("component_disabled");
        }

        private void OnApplicationQuit()
        {
            EndTrial("application_quit");
        }

        private void OnCollisionEnter(Collision collision)
        {
            string other = collision.collider != null ? collision.collider.name : "unknown";
            if (IsIgnoredCollision(other))
            {
                return;
            }

            ContactPoint contact = collision.contactCount > 0 ? collision.GetContact(0) : default;
            pendingCollisions.Add(new CollisionSample
            {
                other = other,
                point = ToArray(contact.point),
                normal = ToArray(contact.normal),
                relative_speed = collision.relativeVelocity.magnitude
            });
        }

        public void BeginTrial(string requestedTrialId = null)
        {
            if (IsRecording)
            {
                return;
            }

            trialId = string.IsNullOrWhiteSpace(requestedTrialId)
                ? DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture)
                : SanitizeFileName(requestedTrialId);
            string root = Path.IsPathRooted(outputDirectory)
                ? outputDirectory
                : Path.Combine(Application.persistentDataPath, outputDirectory);
            Directory.CreateDirectory(root);
            outputPath = Path.Combine(root, "trial-" + trialId + ".jsonl");
            writer = new StreamWriter(outputPath, false, new System.Text.UTF8Encoding(false))
            {
                AutoFlush = true
            };
            startedRealtime = Time.realtimeSinceStartup;
            nextSampleRealtime = startedRealtime;
            stableStopStarted = -1f;
            trajectorySamples = 0;
            collisionCount = 0;
            maximumAvoidCount = 0;
            distanceTravelled = 0f;
            minimumClearanceMetres = float.PositiveInfinity;
            previousPosition = RobotTransform.position;
            latestState = null;
            latestReason = null;
            latestTermination = null;
            latestStatusSequence = -1;
            controllerArrived = false;
            endingRequested = false;
            endingRequestedAt = 0f;
            pendingCollisions.Clear();

            WriteRecord(new TrialStartRecord
            {
                type = "trial_start",
                trial_id = trialId,
                scenario = scenario,
                utc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                scene = gameObject.scene.name,
                unity_version = Application.unityVersion,
                robot_position = ToArray(RobotTransform.position),
                target_position = TargetPositionArray()
            });
        }

        public void EndTrial(string termination = "completed")
        {
            if (!IsRecording)
            {
                return;
            }

            FlushCollisionEvents();
            UpdateStableStop();
            Transform robotTransform = RobotTransform;
            float duration = Time.realtimeSinceStartup - startedRealtime;
            float finalDistance = TrueDistanceMetres(robotTransform.position);
            float finalHeading = TrueHeadingErrorDegrees(robotTransform);
            float stableStopSeconds = CurrentStableStopSeconds();
            bool hasTruth = target != null;
            bool success = hasTruth &&
                           controllerArrived &&
                           collisionCount == 0 &&
                           finalDistance <= maximumFinalDistanceMetres &&
                           finalHeading <= maximumHeadingErrorDegrees &&
                           duration <= maximumDurationSeconds &&
                           stableStopSeconds >= requiredStableStopSeconds;

            WriteRecord(new TrialSummaryRecord
            {
                type = "trial_summary",
                trial_id = trialId,
                scenario = scenario,
                utc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                result = success ? "success" : "failed",
                success = success,
                termination = string.IsNullOrEmpty(latestTermination) ? termination : latestTermination,
                duration_s = duration,
                trajectory_samples = trajectorySamples,
                collision_count = collisionCount,
                path_length_m = distanceTravelled,
                final_distance_m = finalDistance,
                final_heading_error_deg = finalHeading,
                minimum_clearance_m = float.IsPositiveInfinity(minimumClearanceMetres)
                    ? -1f
                    : minimumClearanceMetres,
                avoid_count = maximumAvoidCount,
                stable_stop_s = stableStopSeconds,
                controller_arrived = controllerArrived,
                final_state = latestState,
                final_reason = latestReason,
                final_status_seq = latestStatusSequence,
                robot_position = ToArray(robotTransform.position),
                target_position = TargetPositionArray(),
                robot_yaw_deg = robotTransform.eulerAngles.y
            });
            writer.Dispose();
            writer = null;
        }

        private Transform RobotTransform => robot != null ? robot : transform;

        private void WriteTrajectorySample()
        {
            Transform robotTransform = RobotTransform;
            Vector3 position = robotTransform.position;
            distanceTravelled += PlanarDistance(previousPosition, position);
            previousPosition = position;
            trajectorySamples++;

            Vector3 velocity = robotBody != null ? robotBody.velocity : Vector3.zero;
            Vector3 angularVelocity = robotBody != null ? robotBody.angularVelocity : Vector3.zero;
            Vector2 output = motionDriver != null ? motionDriver.ActualOutput : Vector2.zero;
            int distanceMillimetres = distanceSensor != null
                ? distanceSensor.LatestDistanceMillimetres
                : -1;
            if (distanceMillimetres >= 0)
            {
                minimumClearanceMetres = Mathf.Min(minimumClearanceMetres, distanceMillimetres / 1000f);
            }

            WriteRecord(new TrajectoryRecord
            {
                type = "trajectory",
                trial_id = trialId,
                scenario = scenario,
                time_s = Time.realtimeSinceStartup - startedRealtime,
                robot_position = ToArray(position),
                target_position = TargetPositionArray(),
                robot_yaw_deg = robotTransform.eulerAngles.y,
                true_distance_m = TrueDistanceMetres(position),
                true_heading_error_deg = TrueHeadingErrorDegrees(robotTransform),
                clearance_m = distanceMillimetres >= 0 ? distanceMillimetres / 1000f : -1f,
                rotation_xyzw = new[]
                {
                    robotTransform.rotation.x,
                    robotTransform.rotation.y,
                    robotTransform.rotation.z,
                    robotTransform.rotation.w
                },
                velocity = ToArray(velocity),
                angular_velocity = ToArray(angularVelocity),
                actual_velocity = output.x,
                actual_steer = output.y,
                motion_mode = motionDriver != null ? motionDriver.CurrentMode.ToString() : null,
                distance_mm = distanceMillimetres,
                autonomy_state = latestState,
                autonomy_reason = latestReason,
                autonomy_seq = latestStatusSequence,
                avoid_count = maximumAvoidCount,
                collision_count = collisionCount,
                stable_stop_s = CurrentStableStopSeconds()
            });
        }

        private void FlushCollisionEvents()
        {
            for (int i = 0; i < pendingCollisions.Count; i++)
            {
                CollisionSample sample = pendingCollisions[i];
                collisionCount++;
                WriteRecord(new CollisionRecord
                {
                    type = "collision",
                    trial_id = trialId,
                    scenario = scenario,
                    time_s = Time.realtimeSinceStartup - startedRealtime,
                    other = sample.other,
                    point = sample.point,
                    normal = sample.normal,
                    relative_speed = sample.relative_speed
                });
            }
            pendingCollisions.Clear();
        }

        private void OnStatusUpdated(AutonomyStatusMessage message)
        {
            latestState = message.control_state;
            latestReason = message.@event != null ? message.@event.message : null;
            latestTermination = message.termination;
            latestStatusSequence = message.seq;
            controllerArrived |= string.Equals(message.control_state, "ARRIVED", StringComparison.Ordinal);
            if (message.obstacle != null)
            {
                maximumAvoidCount = Mathf.Max(maximumAvoidCount, message.obstacle.avoid_count);
            }
            if (!IsRecording)
            {
                return;
            }

            WriteRecord(new StatusRecord
            {
                type = "autonomy_status",
                trial_id = trialId,
                scenario = scenario,
                time_s = Time.realtimeSinceStartup - startedRealtime,
                schema_version = message.schema_version,
                session_id = message.session_id,
                seq = message.seq,
                state = message.control_state,
                reason = message.@event != null ? message.@event.message : null,
                action = message.output != null ? message.output.source : null,
                velocity = message.output != null ? message.output.v : 0f,
                steer = message.output != null ? message.output.steer : 0f,
                distance_mm = message.obstacle != null ? message.obstacle.distance_mm : -1f,
                avoid_count = message.obstacle != null ? message.obstacle.avoid_count : 0,
                armed = message.armed,
                termination = message.termination
            });

            if (endWhenAutonomyFinishes && !string.IsNullOrEmpty(message.termination))
            {
                endingRequested = true;
                endingRequestedAt = Time.realtimeSinceStartup;
            }
        }

        private void OnStatusTimedOut()
        {
            latestState = "STALE";
            latestReason = "status_watchdog";
            if (IsRecording)
            {
                WriteRecord(new EventRecord
                {
                    type = "event",
                    trial_id = trialId,
                    scenario = scenario,
                    time_s = Time.realtimeSinceStartup - startedRealtime,
                    name = "autonomy_status_timeout"
                });
            }
        }

        private void UpdateStableStop()
        {
            Vector3 velocity = robotBody != null ? robotBody.velocity : Vector3.zero;
            Vector3 angularVelocity = robotBody != null ? robotBody.angularVelocity : Vector3.zero;
            bool stopped = new Vector2(velocity.x, velocity.z).magnitude <= stoppedLinearSpeed &&
                           angularVelocity.magnitude <= stoppedAngularSpeed;
            if (stopped)
            {
                if (stableStopStarted < 0f)
                {
                    stableStopStarted = Time.realtimeSinceStartup;
                }
            }
            else
            {
                stableStopStarted = -1f;
            }
        }

        private float CurrentStableStopSeconds()
        {
            return stableStopStarted < 0f ? 0f : Time.realtimeSinceStartup - stableStopStarted;
        }

        private float TrueDistanceMetres(Vector3 robotPosition)
        {
            return target == null ? -1f : PlanarDistance(robotPosition, target.position);
        }

        private float TrueHeadingErrorDegrees(Transform robotTransform)
        {
            if (target == null)
            {
                return -1f;
            }

            Vector3 delta = target.position - robotTransform.position;
            float desiredYaw = Mathf.Atan2(delta.x, delta.z) * Mathf.Rad2Deg;
            return Mathf.Abs(Mathf.DeltaAngle(robotTransform.eulerAngles.y, desiredYaw));
        }

        private float[] TargetPositionArray()
        {
            return target == null ? null : ToArray(target.position);
        }

        private bool IsIgnoredCollision(string objectName)
        {
            if (ignoredCollisionObjects == null)
            {
                return false;
            }

            for (int i = 0; i < ignoredCollisionObjects.Length; i++)
            {
                if (string.Equals(objectName, ignoredCollisionObjects[i], StringComparison.Ordinal))
                {
                    return true;
                }
            }
            return false;
        }

        private void WriteRecord(object value)
        {
            writer?.WriteLine(JsonUtility.ToJson(value));
        }

        private static float PlanarDistance(Vector3 a, Vector3 b)
        {
            return Vector2.Distance(new Vector2(a.x, a.z), new Vector2(b.x, b.z));
        }

        private static float[] ToArray(Vector3 value)
        {
            return new[] { value.x, value.y, value.z };
        }

        private static string SanitizeFileName(string value)
        {
            foreach (char invalid in Path.GetInvalidFileNameChars())
            {
                value = value.Replace(invalid, '_');
            }
            return value;
        }

        [Serializable]
        private sealed class TrialStartRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public string utc;
            public string scene;
            public string unity_version;
            public float[] robot_position;
            public float[] target_position;
        }

        [Serializable]
        private sealed class TrajectoryRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public float time_s;
            public float[] robot_position;
            public float[] target_position;
            public float robot_yaw_deg;
            public float true_distance_m;
            public float true_heading_error_deg;
            public float clearance_m;
            public float[] rotation_xyzw;
            public float[] velocity;
            public float[] angular_velocity;
            public float actual_velocity;
            public float actual_steer;
            public string motion_mode;
            public int distance_mm;
            public string autonomy_state;
            public string autonomy_reason;
            public long autonomy_seq;
            public int avoid_count;
            public int collision_count;
            public float stable_stop_s;
        }

        private sealed class CollisionSample
        {
            public string other;
            public float[] point;
            public float[] normal;
            public float relative_speed;
        }

        [Serializable]
        private sealed class CollisionRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public float time_s;
            public string other;
            public float[] point;
            public float[] normal;
            public float relative_speed;
        }

        [Serializable]
        private sealed class StatusRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public float time_s;
            public int schema_version;
            public string session_id;
            public long seq;
            public string state;
            public string reason;
            public string action;
            public float velocity;
            public float steer;
            public float distance_mm;
            public int avoid_count;
            public bool armed;
            public string termination;
        }

        [Serializable]
        private sealed class EventRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public float time_s;
            public string name;
        }

        [Serializable]
        private sealed class TrialSummaryRecord
        {
            public string type;
            public string trial_id;
            public string scenario;
            public string utc;
            public string result;
            public bool success;
            public string termination;
            public float duration_s;
            public int trajectory_samples;
            public int collision_count;
            public float path_length_m;
            public float final_distance_m;
            public float final_heading_error_deg;
            public float minimum_clearance_m;
            public int avoid_count;
            public float stable_stop_s;
            public bool controller_arrived;
            public string final_state;
            public string final_reason;
            public long final_status_seq;
            public float[] robot_position;
            public float[] target_position;
            public float robot_yaw_deg;
        }
    }
}
