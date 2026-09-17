using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Rendering;

public class ExtinguisherSprayController : MonoBehaviour
{
    [Header("Input")]
    public RobotSyncManager robotSyncManager;
    public InputActionReference sprayAction;
    [Range(0.05f, 1f)] public float triggerThreshold = 0.25f;
    public bool enableKeyboardDebug = true;
    public Key keyboardDebugKey = Key.J;

    [Header("Spray")]
    public Transform nozzle;
    public ParticleSystem sprayParticles;
    public Material sprayMaterial;
    public AudioSource sprayAudio;
    public bool autoCreateSprayParticles = true;
    [Min(0.1f)] public float sprayRange = 6f;
    [Range(1f, 45f)] public float sprayConeAngle = 18f;
    [Min(0.1f)] public float extinguishPower = 1f;

    [Header("Fire Control")]
    public LightControl fireControl;

    public bool IsSpraying { get; private set; }
    public bool IsHittingFire { get; private set; }

    private readonly List<ParticleSystem> _sprayLayers = new List<ParticleSystem>();

    private void Awake()
    {
        if (nozzle == null)
            nozzle = transform;

        if (robotSyncManager == null)
            robotSyncManager = FindFirstObjectByType<RobotSyncManager>();

        if (fireControl == null)
            fireControl = FindFirstObjectByType<LightControl>();

        if (sprayParticles == null && autoCreateSprayParticles)
            sprayParticles = CreateDefaultSprayParticles();

        if (sprayParticles != null)
        {
            EnsureSprayMaterial(sprayParticles);
            RegisterSprayLayer(sprayParticles);
        }
    }

    private void OnEnable()
    {
        if (sprayAction != null && sprayAction.action != null)
            sprayAction.action.Enable();
    }

    private void OnDisable()
    {
        SetSprayVisuals(false);
    }

    private void Update()
    {
        bool shouldSpray = ReadSprayHeld();
        SetSprayVisuals(shouldSpray);

        IsHittingFire = false;
        if (shouldSpray && fireControl != null && nozzle != null)
        {
            IsHittingFire = fireControl.ApplySpray(
                nozzle.position,
                nozzle.forward,
                sprayRange,
                sprayConeAngle,
                extinguishPower * Time.deltaTime);
        }
    }

    private bool ReadSprayHeld()
    {
        InputActionReference action = sprayAction;
        if ((action == null || action.action == null) && robotSyncManager != null)
            action = robotSyncManager.rightTriggerAction;

        if (action != null && action.action != null && action.action.enabled)
        {
            try
            {
                if (action.action.ReadValue<float>() >= triggerThreshold)
                    return true;
            }
            catch
            {
                // Ignore bindings that are not scalar buttons.
            }
        }

        return enableKeyboardDebug &&
               Keyboard.current != null &&
               Keyboard.current[keyboardDebugKey].isPressed;
    }

    private void SetSprayVisuals(bool active)
    {
        if (IsSpraying == active)
            return;

        IsSpraying = active;

        foreach (var layer in _sprayLayers)
        {
            if (layer == null)
                continue;

            if (active)
                layer.Play(true);
            else
                layer.Stop(true, ParticleSystemStopBehavior.StopEmitting);
        }

        if (sprayAudio != null)
        {
            if (active && !sprayAudio.isPlaying)
                sprayAudio.Play();
            else if (!active && sprayAudio.isPlaying)
                sprayAudio.Stop();
        }
    }

    private ParticleSystem CreateDefaultSprayParticles()
    {
        var sprayObject = new GameObject("Extinguisher Spray - Pressure Core");
        sprayObject.transform.SetParent(nozzle, false);

        var particles = sprayObject.AddComponent<ParticleSystem>();
        var main = particles.main;
        main.loop = true;
        main.playOnAwake = false;
        main.startLifetime = new ParticleSystem.MinMaxCurve(0.42f, 0.62f);
        main.startSpeed = new ParticleSystem.MinMaxCurve(10f, 13f);
        main.startSize = new ParticleSystem.MinMaxCurve(0.025f, 0.065f);
        main.startColor = new Color(0.97f, 0.98f, 0.96f, 0.92f);
        main.simulationSpace = ParticleSystemSimulationSpace.World;
        main.maxParticles = 240;

        var emission = particles.emission;
        emission.rateOverTime = 150f;

        var shape = particles.shape;
        shape.shapeType = ParticleSystemShapeType.Cone;
        shape.angle = 8f;
        shape.radius = 0.018f;

        var noise = particles.noise;
        noise.enabled = true;
        noise.strength = 0.18f;
        noise.frequency = 2f;
        noise.scrollSpeed = 0.55f;

        var colorOverLifetime = particles.colorOverLifetime;
        colorOverLifetime.enabled = true;
        var colorGradient = new Gradient();
        colorGradient.SetKeys(
            new[]
            {
                new GradientColorKey(new Color(1f, 1f, 0.98f), 0f),
                new GradientColorKey(new Color(0.86f, 0.88f, 0.84f), 1f)
            },
            new[]
            {
                new GradientAlphaKey(0.92f, 0f),
                new GradientAlphaKey(0.72f, 0.45f),
                new GradientAlphaKey(0f, 1f)
            });
        colorOverLifetime.color = colorGradient;

        var sizeOverLifetime = particles.sizeOverLifetime;
        sizeOverLifetime.enabled = true;
        sizeOverLifetime.size = new ParticleSystem.MinMaxCurve(
            1f,
            new AnimationCurve(
                new Keyframe(0f, 0.65f),
                new Keyframe(0.45f, 0.9f),
                new Keyframe(1f, 1.25f)));

        var limitVelocity = particles.limitVelocityOverLifetime;
        limitVelocity.enabled = true;
        limitVelocity.limit = 10f;
        limitVelocity.dampen = 0.18f;

        EnsureSprayMaterial(particles);
        RegisterSprayLayer(particles);
        CreatePowderMistLayer();
        CreateResidualDustLayer();

        particles.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
        return particles;
    }

    private void CreatePowderMistLayer()
    {
        var particles = CreateLayerObject("Extinguisher Spray - Expanding Powder");
        var main = particles.main;
        main.startLifetime = new ParticleSystem.MinMaxCurve(0.65f, 1f);
        main.startSpeed = new ParticleSystem.MinMaxCurve(6.5f, 9f);
        main.startSize = new ParticleSystem.MinMaxCurve(0.07f, 0.18f);
        main.startColor = new Color(0.92f, 0.93f, 0.88f, 0.62f);
        main.maxParticles = 320;

        var emission = particles.emission;
        emission.rateOverTime = 180f;

        ConfigureCone(particles, sprayConeAngle, 0.035f);
        ConfigureNoise(particles, 0.48f, 1.25f, 0.35f);
        ConfigureFadeAndGrowth(particles, 0.68f, 1.85f);

        var limitVelocity = particles.limitVelocityOverLifetime;
        limitVelocity.enabled = true;
        limitVelocity.limit = 5.5f;
        limitVelocity.dampen = 0.32f;

        FinalizeLayer(particles);
    }

    private void CreateResidualDustLayer()
    {
        var particles = CreateLayerObject("Extinguisher Spray - Residual Dust");
        var main = particles.main;
        main.startLifetime = new ParticleSystem.MinMaxCurve(0.9f, 1.5f);
        main.startSpeed = new ParticleSystem.MinMaxCurve(3.5f, 6f);
        main.startSize = new ParticleSystem.MinMaxCurve(0.12f, 0.28f);
        main.startColor = new Color(0.82f, 0.83f, 0.78f, 0.38f);
        main.gravityModifier = 0.12f;
        main.maxParticles = 180;

        var emission = particles.emission;
        emission.rateOverTime = 65f;

        ConfigureCone(particles, 22f, 0.05f);
        ConfigureNoise(particles, 0.72f, 0.75f, 0.22f);
        ConfigureFadeAndGrowth(particles, 0.38f, 2.2f);

        var limitVelocity = particles.limitVelocityOverLifetime;
        limitVelocity.enabled = true;
        limitVelocity.limit = 2.8f;
        limitVelocity.dampen = 0.42f;

        FinalizeLayer(particles);
    }

    private ParticleSystem CreateLayerObject(string layerName)
    {
        var layerObject = new GameObject(layerName);
        layerObject.transform.SetParent(nozzle, false);

        var particles = layerObject.AddComponent<ParticleSystem>();
        var main = particles.main;
        main.loop = true;
        main.playOnAwake = false;
        main.simulationSpace = ParticleSystemSimulationSpace.World;
        return particles;
    }

    private static void ConfigureCone(ParticleSystem particles, float angle, float radius)
    {
        var shape = particles.shape;
        shape.shapeType = ParticleSystemShapeType.Cone;
        shape.angle = angle;
        shape.radius = radius;
    }

    private static void ConfigureNoise(
        ParticleSystem particles,
        float strength,
        float frequency,
        float scrollSpeed)
    {
        var noise = particles.noise;
        noise.enabled = true;
        noise.strength = strength;
        noise.frequency = frequency;
        noise.scrollSpeed = scrollSpeed;
    }

    private static void ConfigureFadeAndGrowth(
        ParticleSystem particles,
        float peakAlpha,
        float endScale)
    {
        var colorOverLifetime = particles.colorOverLifetime;
        colorOverLifetime.enabled = true;
        var gradient = new Gradient();
        gradient.SetKeys(
            new[]
            {
                new GradientColorKey(Color.white, 0f),
                new GradientColorKey(new Color(0.82f, 0.84f, 0.8f), 1f)
            },
            new[]
            {
                new GradientAlphaKey(0f, 0f),
                new GradientAlphaKey(peakAlpha, 0.12f),
                new GradientAlphaKey(peakAlpha * 0.75f, 0.58f),
                new GradientAlphaKey(0f, 1f)
            });
        colorOverLifetime.color = gradient;

        var sizeOverLifetime = particles.sizeOverLifetime;
        sizeOverLifetime.enabled = true;
        sizeOverLifetime.size = new ParticleSystem.MinMaxCurve(
            1f,
            new AnimationCurve(
                new Keyframe(0f, 0.35f),
                new Keyframe(0.25f, 0.9f),
                new Keyframe(1f, endScale)));
    }

    private void FinalizeLayer(ParticleSystem particles)
    {
        EnsureSprayMaterial(particles);
        RegisterSprayLayer(particles);
        particles.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
    }

    private void RegisterSprayLayer(ParticleSystem particles)
    {
        if (particles != null && !_sprayLayers.Contains(particles))
            _sprayLayers.Add(particles);
    }

    private void EnsureSprayMaterial(ParticleSystem particles)
    {
        var particleRenderer = particles.GetComponent<ParticleSystemRenderer>();
        if (particleRenderer == null)
            return;

        particleRenderer.renderMode = ParticleSystemRenderMode.Billboard;
        particleRenderer.alignment = ParticleSystemRenderSpace.View;
        particleRenderer.shadowCastingMode = ShadowCastingMode.Off;
        particleRenderer.receiveShadows = false;

        bool isBuiltInRenderPipeline = GraphicsSettings.currentRenderPipeline == null;
        if (IsMaterialCompatible(sprayMaterial, isBuiltInRenderPipeline))
        {
            particleRenderer.sharedMaterial = sprayMaterial;
            return;
        }

        sprayMaterial = null;
        Shader shader = null;

        if (isBuiltInRenderPipeline)
        {
            shader = Shader.Find("Particles/Standard Unlit");
            if (shader == null || !shader.isSupported)
                shader = Shader.Find("Legacy Shaders/Particles/Alpha Blended");
        }
        else
        {
            shader = Shader.Find("Universal Render Pipeline/Particles/Unlit");
        }

        if (shader == null || !shader.isSupported)
            shader = Shader.Find("Unlit/Transparent");

        if (shader == null || !shader.isSupported)
        {
            Debug.LogWarning("[ExtinguisherSpray] No compatible transparent particle shader was found.");
            return;
        }

        sprayMaterial = new Material(shader)
        {
            name = "Runtime Extinguisher Spray Material",
            renderQueue = 3000
        };

        if (sprayMaterial.HasProperty("_BaseColor"))
            sprayMaterial.SetColor("_BaseColor", new Color(0.9f, 0.94f, 0.96f, 0.72f));
        if (sprayMaterial.HasProperty("_Color"))
            sprayMaterial.SetColor("_Color", new Color(0.9f, 0.94f, 0.96f, 0.72f));
        if (sprayMaterial.HasProperty("_Surface"))
            sprayMaterial.SetFloat("_Surface", 1f);
        if (sprayMaterial.HasProperty("_Blend"))
            sprayMaterial.SetFloat("_Blend", 0f);
        if (sprayMaterial.HasProperty("_ZWrite"))
            sprayMaterial.SetFloat("_ZWrite", 0f);

        if (isBuiltInRenderPipeline)
        {
            if (sprayMaterial.HasProperty("_Mode"))
                sprayMaterial.SetFloat("_Mode", 2f);
            if (sprayMaterial.HasProperty("_SrcBlend"))
                sprayMaterial.SetFloat("_SrcBlend", (float)BlendMode.SrcAlpha);
            if (sprayMaterial.HasProperty("_DstBlend"))
                sprayMaterial.SetFloat("_DstBlend", (float)BlendMode.OneMinusSrcAlpha);

            sprayMaterial.DisableKeyword("_ALPHATEST_ON");
            sprayMaterial.DisableKeyword("_ALPHAPREMULTIPLY_ON");
            sprayMaterial.EnableKeyword("_ALPHABLEND_ON");
        }

        particleRenderer.sharedMaterial = sprayMaterial;
    }

    private static bool IsMaterialCompatible(Material material, bool isBuiltInRenderPipeline)
    {
        if (material == null || material.shader == null || !material.shader.isSupported)
            return false;

        string shaderName = material.shader.name;
        if (isBuiltInRenderPipeline)
            return !shaderName.StartsWith("Universal Render Pipeline/");

        return shaderName.StartsWith("Universal Render Pipeline/");
    }

    private void OnDrawGizmosSelected()
    {
        Transform source = nozzle != null ? nozzle : transform;
        Vector3 origin = source.position;
        Vector3 forward = source.forward;
        float radius = Mathf.Tan(sprayConeAngle * Mathf.Deg2Rad) * sprayRange;

        Gizmos.color = IsHittingFire ? Color.green : Color.cyan;
        Gizmos.DrawLine(origin, origin + forward * sprayRange);
        Gizmos.DrawWireSphere(origin + forward * sprayRange, radius);
    }
}
