using System;
using System.Reflection;
using UnityEngine;

namespace HciRobot.Simulator
{
    /// <summary>
    /// Adapts the course's one-shot right_grip/outfire command to its existing
    /// LightControl component without taking a compile-time dependency on the
    /// course project.
    /// </summary>
    [DisallowMultipleComponent]
    public sealed class CourseFireExtinguishAdapter : MonoBehaviour
    {
        private const string TriggerMethodName = "OnActionTriggered";
        private const string InstantFieldName = "legacyInstantExtinguishOnAction";
        private const string DistanceFieldName = "extinguishDistance";
        private const BindingFlags MemberFlags = BindingFlags.Instance
            | BindingFlags.Public | BindingFlags.NonPublic;

        [SerializeField] private TonyPiMotionDriver motionDriver;
        [Tooltip("Optional explicit course LightControl. Leave empty to discover it by its compatibility contract.")]
        [SerializeField] private MonoBehaviour fireControl;
        [SerializeField] private string commandAction = "right_grip";
        [SerializeField] private string actionGroupAlias = "outfire";
        [Tooltip("Maximum real-world distance at which the discrete extinguish action may affect a fire.")]
        [SerializeField, Min(0f)] private float extinguishRangeMetres = 1.2f;
        [SerializeField] private bool autoDiscoverFireControl = true;

        public MonoBehaviour FireControl
        {
            get => fireControl;
            set => fireControl = value;
        }

        public float ExtinguishRangeMetres
        {
            get => extinguishRangeMetres;
            set => extinguishRangeMetres = Mathf.Max(0f, value);
        }

        public bool LastRequestDispatched { get; private set; }

        private void Awake()
        {
            ResolveDriver();
        }

        private void OnEnable()
        {
            ResolveDriver();
            if (motionDriver == null)
                return;

            // Remove first so editor lifecycle re-entry cannot double-subscribe.
            motionDriver.ActionReceived -= OnActionReceived;
            motionDriver.ActionReceived += OnActionReceived;
        }

        private void OnDisable()
        {
            if (motionDriver != null)
                motionDriver.ActionReceived -= OnActionReceived;
        }

        private void ResolveDriver()
        {
            if (motionDriver == null)
                motionDriver = GetComponent<TonyPiMotionDriver>();
        }

        private void OnActionReceived(string action)
        {
            TryDispatch(action);
        }

        public bool TryDispatch(string action)
        {
            LastRequestDispatched = false;
            if (!MatchesAction(action))
                return false;

            // TryDispatch is also a public integration hook and may be called
            // before Unity has invoked this component's Awake/OnEnable methods.
            ResolveDriver();

            MonoBehaviour target = ResolveFireControl();
            if (target == null)
            {
                Debug.LogWarning(
                    "[HCIRobot Fire] Extinguish action received, but no compatible course LightControl was found.",
                    this);
                return false;
            }

            Type targetType = target.GetType();
            MethodInfo trigger = targetType.GetMethod(TriggerMethodName, MemberFlags, null, Type.EmptyTypes, null);
            if (trigger == null)
            {
                Debug.LogWarning(
                    $"[HCIRobot Fire] '{targetType.Name}' has no compatible {TriggerMethodName} method.",
                    target);
                return false;
            }

            SetFieldIfCompatible(target, targetType, InstantFieldName, true);

            float sceneUnitsPerMetre = motionDriver != null
                ? motionDriver.MovementSpeedMultiplier
                : 1f;
            float sceneRange = Mathf.Max(0f, extinguishRangeMetres) * Mathf.Max(0f, sceneUnitsPerMetre);
            SetFieldIfCompatible(target, targetType, DistanceFieldName, sceneRange);

            try
            {
                trigger.Invoke(target, null);
                LastRequestDispatched = true;
                Debug.Log(
                    $"[HCIRobot Fire] Dispatched {action}; allowed range {extinguishRangeMetres:F2} m ({sceneRange:F2} scene units).",
                    target);
                return true;
            }
            catch (TargetInvocationException exception)
            {
                Exception cause = exception.InnerException ?? exception;
                Debug.LogWarning($"[HCIRobot Fire] Extinguish dispatch failed: {cause.Message}", target);
                return false;
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[HCIRobot Fire] Extinguish dispatch failed: {exception.Message}", target);
                return false;
            }
        }

        private bool MatchesAction(string action)
        {
            return !string.IsNullOrWhiteSpace(action)
                && (string.Equals(action, commandAction, StringComparison.OrdinalIgnoreCase)
                    || string.Equals(action, actionGroupAlias, StringComparison.OrdinalIgnoreCase));
        }

        private MonoBehaviour ResolveFireControl()
        {
            if (fireControl != null)
                return fireControl;
            if (!autoDiscoverFireControl)
                return null;

            foreach (MonoBehaviour candidate in FindObjectsOfType<MonoBehaviour>(true))
            {
                if (candidate == null || !candidate.gameObject.scene.IsValid())
                    continue;
                Type type = candidate.GetType();
                MethodInfo trigger = type.GetMethod(TriggerMethodName, MemberFlags, null, Type.EmptyTypes, null);
                if (trigger == null)
                    continue;

                bool isNamedCourseController = string.Equals(type.Name, "LightControl", StringComparison.Ordinal);
                bool hasCourseFields = type.GetField(InstantFieldName, MemberFlags)?.FieldType == typeof(bool)
                    && type.GetField(DistanceFieldName, MemberFlags)?.FieldType == typeof(float);
                if (!isNamedCourseController && !hasCourseFields)
                    continue;

                fireControl = candidate;
                return fireControl;
            }
            return null;
        }

        private static void SetFieldIfCompatible(
            MonoBehaviour target,
            Type targetType,
            string fieldName,
            object value)
        {
            FieldInfo field = targetType.GetField(fieldName, MemberFlags);
            if (field != null && field.FieldType.IsInstanceOfType(value))
                field.SetValue(target, value);
        }
    }
}
