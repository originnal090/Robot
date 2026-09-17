using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class LightControl : MonoBehaviour
{
    [Header("灯光提示")]
    public Light pointLight;
    public float blinkInterval = 1.0f;
    public Color blinkColor = Color.red;

    [Header("RobotSync")]
    public RobotSyncManager robotSyncManager;

    [Header("火焰设置")]
    public GameObject firePrefab;                  // 运行时自动生成的火焰预制体
    public Transform defaultFireAnchor;           // 场景开始运行时自动点燃的火点
    // 保留旧场景的序列化配置；自动生成只读取 defaultFireAnchor。
    public List<Transform> fireAnchors = new List<Transform>();
    public bool onlyOneFireAtATime = true;
    public bool lockRespawnAfterExtinguish = true;
    public bool parentFireToAnchor = false;
    public float spawnYOffset = 0.02f;
    [Min(0.1f)] public float fireScaleMultiplier = 1f;
    public Vector3 fireVisualScale = new Vector3(2f, 1.3f, 2f);
    [Min(0f)] public float fireEmissionMultiplier = 2f;
    [Min(0f)] public float fireLightIntensityMultiplier = 1.5f;
    public bool autoRegisterChildFires = true;

    [Header("熄灭判定")]
    public Transform robotRoot;                   // 机器人在 Unity 场景中的根节点
    public float extinguishDistance = 1.2f;       // 机器人靠近到这个距离内才允许灭火

    [Header("Progressive Extinguishing")]
    public bool legacyInstantExtinguishOnAction = false;
    [Min(0.1f)] public float requiredSpraySeconds = 3f;
    [Range(0.05f, 1f)] public float minimumFireScale = 0.2f;
    [Min(0f)] public float smokeLingerSeconds = 2f;

    private bool _isBlinking = false;
    private Coroutine _blinkCoroutine;
    private Color _originalColor;

    private readonly Dictionary<string, GameObject> _activeFires = new Dictionary<string, GameObject>();
    private readonly HashSet<string> _extinguishedTargets = new HashSet<string>();
    private readonly Dictionary<string, FireVisualState> _fireVisualStates = new Dictionary<string, FireVisualState>();

    private class FireVisualState
    {
        public Vector3 initialScale;
        public float sprayedSeconds;
        public ParticleSystem[] particles;
        public float[] rateOverTimeMultipliers;
        public float[] rateOverDistanceMultipliers;
        public Light[] lights;
        public float[] lightIntensities;
    }

    private const string DefaultKey = "__default__";

    void Start()
    {
        if (pointLight != null)
        {
            _originalColor = pointLight.color;
            pointLight.enabled = false;
        }

        AutoAssignRobotSyncManager();
        RegisterExistingChildFires();
        SpawnDefaultFire();

        if (robotSyncManager != null)
        {
            robotSyncManager.onActionTriggered.AddListener(OnActionTriggered);
        }

        if (_activeFires.Count > 0)
        {
            StartBlinking();
        }
    }

    void OnDestroy()
    {
        if (robotSyncManager != null)
        {
            robotSyncManager.onActionTriggered.RemoveListener(OnActionTriggered);
        }

        StopBlinking();
        DestroyAllFires();
    }

    private void AutoAssignRobotSyncManager()
    {
        if (robotSyncManager != null)
            return;

#if UNITY_6000_0_OR_NEWER
        robotSyncManager = FindFirstObjectByType<RobotSyncManager>();
#else
        robotSyncManager = FindObjectOfType<RobotSyncManager>();
#endif
    }

    private void RegisterExistingChildFires()
    {
        if (!autoRegisterChildFires)
            return;

        foreach (Transform child in transform)
        {
            if (child == null || child.GetComponentInChildren<ParticleSystem>(true) == null)
                continue;

            GameObject childFire = child.gameObject;
            if (!childFire.activeInHierarchy || _activeFires.ContainsValue(childFire))
                continue;

            string key = BuildUniqueFireKey(childFire.name);
            _activeFires[key] = childFire;
            RegisterFireVisualState(key, childFire);
        }
    }

    private string BuildUniqueFireKey(string baseKey)
    {
        string key = NormalizeTargetKey(baseKey);
        if (!_activeFires.ContainsKey(key))
            return key;

        int suffix = 1;
        string candidate = key;
        while (_activeFires.ContainsKey(candidate))
        {
            candidate = $"{key}_{suffix++}";
        }

        return candidate;
    }

    private void SpawnDefaultFire()
    {
        if (_activeFires.Count > 0)
            return;

        if (firePrefab == null)
        {
            Debug.LogWarning("[LightControl] firePrefab 未设置，无法自动生成火焰。");
            return;
        }

        if (defaultFireAnchor == null)
        {
            Debug.LogWarning("[LightControl] defaultFireAnchor 未设置，无法自动生成火焰。");
            return;
        }

        Vector3 spawnPos = defaultFireAnchor.position + Vector3.up * spawnYOffset;
        Quaternion spawnRot = defaultFireAnchor.rotation;

        GameObject fire = Instantiate(firePrefab, spawnPos, spawnRot);
        fire.name = firePrefab.name;

        if (parentFireToAnchor)
            fire.transform.SetParent(defaultFireAnchor, true);

        fire.transform.localScale *= Mathf.Max(0.1f, fireScaleMultiplier);
        ApplyFireVisualBoost(fire);

        _activeFires[DefaultKey] = fire;
        RegisterFireVisualState(DefaultKey, fire);

        StartBlinking();
        Debug.Log($"[LightControl] Auto-spawned fire '{fire.name}' at '{defaultFireAnchor.name}'");
    }

    private void ApplyFireVisualBoost(GameObject fire)
    {
        if (fire == null)
            return;

        Vector3 safeScale = new Vector3(
            Mathf.Max(0.1f, fireVisualScale.x),
            Mathf.Max(0.1f, fireVisualScale.y),
            Mathf.Max(0.1f, fireVisualScale.z));
        fire.transform.localScale = Vector3.Scale(fire.transform.localScale, safeScale);

        float emissionMultiplier = Mathf.Max(0f, fireEmissionMultiplier);
        foreach (var particle in fire.GetComponentsInChildren<ParticleSystem>(true))
        {
            var emission = particle.emission;
            emission.rateOverTimeMultiplier *= emissionMultiplier;
            emission.rateOverDistanceMultiplier *= emissionMultiplier;
        }

        float lightMultiplier = Mathf.Max(0f, fireLightIntensityMultiplier);
        foreach (var fireLight in fire.GetComponentsInChildren<Light>(true))
            fireLight.intensity *= lightMultiplier;
    }

    private void OnActionTriggered()
    {
        if (!legacyInstantExtinguishOnAction)
            return;

        if (_activeFires.Count == 0)
        {
            Debug.Log("[LightControl] 当前没有火焰，无需处理。");
            return;
        }

        string nearestKey = FindNearestFireKeyInRange();
        if (string.IsNullOrEmpty(nearestKey))
        {
            Debug.Log("[LightControl] 已触发动作，但机器人还未靠近任何火焰。");
            return;
        }

        ExtinguishFire(nearestKey);
    }

    public bool ApplySpray(Vector3 origin, Vector3 direction, float range, float coneAngle, float spraySeconds)
    {
        if (_activeFires.Count == 0 || spraySeconds <= 0f || direction.sqrMagnitude < 0.001f)
            return false;

        direction.Normalize();
        string bestKey = null;
        float bestDistance = float.MaxValue;

        foreach (var pair in _activeFires)
        {
            GameObject fire = pair.Value;
            if (fire == null)
                continue;

            Vector3 toFire = fire.transform.position - origin;
            float distance = toFire.magnitude;
            if (distance > range || distance <= 0.001f)
                continue;

            float angle = Vector3.Angle(direction, toFire / distance);
            if (angle <= coneAngle && distance < bestDistance)
            {
                bestKey = pair.Key;
                bestDistance = distance;
            }
        }

        if (string.IsNullOrEmpty(bestKey))
            return false;

        ApplySprayProgress(bestKey, spraySeconds);
        return true;
    }

    private void RegisterFireVisualState(string key, GameObject fire)
    {
        if (fire == null)
            return;

        ParticleSystem[] particles = fire.GetComponentsInChildren<ParticleSystem>(true);
        Light[] lights = fire.GetComponentsInChildren<Light>(true);
        var state = new FireVisualState
        {
            initialScale = fire.transform.localScale,
            sprayedSeconds = 0f,
            particles = particles,
            rateOverTimeMultipliers = new float[particles.Length],
            rateOverDistanceMultipliers = new float[particles.Length],
            lights = lights,
            lightIntensities = new float[lights.Length]
        };

        for (int i = 0; i < particles.Length; i++)
        {
            var emission = particles[i].emission;
            state.rateOverTimeMultipliers[i] = emission.rateOverTimeMultiplier;
            state.rateOverDistanceMultipliers[i] = emission.rateOverDistanceMultiplier;
        }

        for (int i = 0; i < lights.Length; i++)
        {
            if (lights[i] != null)
                state.lightIntensities[i] = lights[i].intensity;
        }

        _fireVisualStates[key] = state;
    }

    private void ApplySprayProgress(string key, float spraySeconds)
    {
        if (!_activeFires.TryGetValue(key, out var fire) || fire == null)
            return;

        if (!_fireVisualStates.TryGetValue(key, out var state))
        {
            RegisterFireVisualState(key, fire);
            state = _fireVisualStates[key];
        }

        state.sprayedSeconds += spraySeconds;
        float progress = Mathf.Clamp01(state.sprayedSeconds / Mathf.Max(0.1f, requiredSpraySeconds));
        float visualStrength = Mathf.Lerp(1f, minimumFireScale, progress);
        fire.transform.localScale = state.initialScale * visualStrength;

        for (int i = 0; i < state.particles.Length; i++)
        {
            if (state.particles[i] == null)
                continue;

            var emission = state.particles[i].emission;
            emission.rateOverTimeMultiplier = state.rateOverTimeMultipliers[i] * visualStrength;
            emission.rateOverDistanceMultiplier = state.rateOverDistanceMultipliers[i] * visualStrength;
        }

        for (int i = 0; i < state.lights.Length; i++)
        {
            if (state.lights[i] != null)
                state.lights[i].intensity = state.lightIntensities[i] * visualStrength;
        }

        if (progress >= 1f)
            ExtinguishFire(key);
    }

    private string FindNearestFireKeyInRange()
    {
        if (robotRoot == null)
        {
            foreach (var kv in _activeFires)
            {
                if (kv.Value != null) return kv.Key;
            }
            return null;
        }

        float bestDist = float.MaxValue;
        string bestKey = null;
        Vector3 robotPos = robotRoot.position;

        foreach (var kv in _activeFires)
        {
            if (kv.Value == null) continue;

            float dist = Vector3.Distance(robotPos, kv.Value.transform.position);
            if (dist <= extinguishDistance && dist < bestDist)
            {
                bestDist = dist;
                bestKey = kv.Key;
            }
        }

        return bestKey;
    }

    private void ExtinguishFire(string key)
    {
        if (!_activeFires.TryGetValue(key, out var fire))
            return;

        if (fire != null)
        {
            foreach (var particle in fire.GetComponentsInChildren<ParticleSystem>(true))
            {
                particle.Stop(true, ParticleSystemStopBehavior.StopEmitting);
            }

            Destroy(fire, smokeLingerSeconds);
        }

        _activeFires.Remove(key);
        _fireVisualStates.Remove(key);

        if (lockRespawnAfterExtinguish)
            _extinguishedTargets.Add(key);

        if (_activeFires.Count == 0)
            StopBlinking();

        Debug.Log($"[LightControl] 火焰 {key} 已熄灭。");
    }

    public void ResetExtinguishedTargets()
    {
        _extinguishedTargets.Clear();
        Debug.Log("[LightControl] 已清空已处理火点列表，可再次生成火焰。");
    }

    private string NormalizeTargetKey(string targetId)
    {
        if (string.IsNullOrWhiteSpace(targetId))
            return DefaultKey;

        return targetId.Trim();
    }

    private void DestroyAllFires()
    {
        var keys = new List<string>(_activeFires.Keys);
        foreach (var key in keys)
        {
            if (_activeFires[key] != null)
                Destroy(_activeFires[key]);
        }
        _activeFires.Clear();
        _fireVisualStates.Clear();
    }

    private void StartBlinking()
    {
        if (_isBlinking) return;

        _isBlinking = true;
        if (_blinkCoroutine != null) StopCoroutine(_blinkCoroutine);
        _blinkCoroutine = StartCoroutine(BlinkRoutine());
    }

    private void StopBlinking()
    {
        if (!_isBlinking) return;

        _isBlinking = false;
        if (_blinkCoroutine != null)
        {
            StopCoroutine(_blinkCoroutine);
            _blinkCoroutine = null;
        }

        if (pointLight != null)
        {
            pointLight.enabled = false;
            pointLight.color = _originalColor;
        }
    }

    private IEnumerator BlinkRoutine()
    {
        if (pointLight == null) yield break;

        pointLight.enabled = true;

        while (_isBlinking)
        {
            pointLight.color = blinkColor;
            yield return new WaitForSeconds(blinkInterval);
            pointLight.enabled = !pointLight.enabled;
            yield return new WaitForSeconds(blinkInterval);
        }

        pointLight.enabled = false;
        pointLight.color = _originalColor;
    }
}


