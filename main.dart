import 'dart:convert';
import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:just_audio/just_audio.dart';
import 'package:audio_session/audio_session.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:palette_generator/palette_generator.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const AetherLosslessApp());
}

class AetherLosslessApp extends StatelessWidget {
  const AetherLosslessApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Aether Lossless',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF07080C),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFFFFD60A),
          secondary: Color(0xFF6366F1),
        ),
      ),
      home: const LosslessHomeScreen(),
    );
  }
}

class Track {
  final int msgId;
  final String title;
  final String artist;
  final String album;
  final String genre;
  final double duration;
  final int bitDepth;
  final int sampleRate;

  Track({
    required this.msgId,
    required this.title,
    required this.artist,
    required this.album,
    required this.genre,
    required this.duration,
    required this.bitDepth,
    required this.sampleRate,
  });

  factory Track.fromJson(Map<String, dynamic> j) {
    return Track(
      msgId: j['msg_id'] ?? 0,
      title: j['title'] ?? 'Unknown Title',
      artist: j['artist'] ?? 'Unknown Artist',
      album: j['album'] ?? 'Single',
      genre: j['genre'] ?? 'Lossless',
      duration: (j['duration'] ?? 0).toDouble(),
      bitDepth: j['bit_depth'] ?? 16,
      sampleRate: j['sample_rate'] ?? 44100,
    );
  }

  String get hiResBadge {
    final khz = (sampleRate / 1000).toStringAsFixed(1).replaceAll('.0', '');
    return '$bitDepth-BIT • ${khz}kHz FLAC';
  }
}

class LyricLine {
  final Duration time;
  final String text;
  LyricLine(this.time, this.text);
}

class LosslessHomeScreen extends StatefulWidget {
  const LosslessHomeScreen({super.key});

  @override
  State<LosslessHomeScreen> createState() => _LosslessHomeScreenState();
}

class _LosslessHomeScreenState extends State<LosslessHomeScreen> {
  // Native Android Hardware Equalizer & Loudness/Spatial Enhancer via ExoPlayer
  final AndroidEqualizer _equalizer = AndroidEqualizer();
  final AndroidLoudnessEnhancer _spatialEnhancer = AndroidLoudnessEnhancer();
  late final AudioPlayer _player;

  String _serverUrl = "http://127.0.0.1:7860";
  List<Track> _catalogue = [];
  List<Track> _queue = [];
  Track? _currentTrack;
  int _selectedTab = 0; // 0: Songs, 1: Albums, 2: 24-Bit Hi-Res, 3: Lyrics, 4: Equalizer
  String _searchQuery = "";
  bool _bitPerfectMode = true;
  bool _spatialEnabled = false;
  List<LyricLine> _lyrics = [];
  Color _dominantColor = const Color(0xFF1E1B4B);
  Color _secondaryColor = const Color(0xFF0F172A);

  @override
  void initState() {
    super.initState();
    _player = AudioPlayer(
      audioPipeline: AudioPipeline(
        androidAudioEffects: [_equalizer, _spatialEnhancer],
      ),
    );
    _initAudioSession();
    _loadSavedServer();
  }

  Future<void> _initAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.music());
    await _equalizer.setEnabled(false); // Default to Bit-Perfect untouched stream
    await _spatialEnhancer.setEnabled(false);

    _player.processingStateStream.listen((state) {
      if (state == ProcessingState.completed) {
        _skipNext();
      }
    });
  }

  Future<void> _loadSavedServer() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _serverUrl = prefs.getString('server_url') ?? "http://127.0.0.1:7860";
    });
    _fetchCatalogue();
  }

  Future<void> _fetchCatalogue() async {
    try {
      final res = await http.get(Uri.parse('$_serverUrl/api/catalogue'));
      if (res.statusCode == 200) {
        final List data = jsonDecode(res.body);
        setState(() {
          _catalogue = data.map((e) => Track.fromJson(e)).toList();
          _queue = List.from(_catalogue);
        });
      }
    } catch (_) {
      // Prompt setup if server is not yet connected
    }
  }

  Future<void> _playTrack(Track track) async {
    setState(() => _currentTrack = track);
    final streamUrl = '$_serverUrl/api/stream/${track.msgId}';
    final coverUrl = '$_serverUrl/api/cover/${track.msgId}';

    _updateDynamicGlassColors(coverUrl);
    _fetchSyncedLyrics(track.title, track.artist);

    await _player.setAudioSource(
      AudioSource.uri(
        Uri.parse(streamUrl),
        tag: track,
      ),
    );
    _player.play();
  }

  Future<void> _updateDynamicGlassColors(String imageUrl) async {
    try {
      final palette = await PaletteGenerator.fromImageProvider(
        CachedNetworkImageProvider(imageUrl),
      );
      setState(() {
        _dominantColor = palette.vibrantColor?.color ??
            palette.dominantColor?.color ??
            const Color(0xFF1E1B4B);
        _secondaryColor = palette.darkMutedColor?.color ?? const Color(0xFF090D16);
      });
    } catch (_) {}
  }

  Future<void> _fetchSyncedLyrics(String title, String artist) async {
    setState(() => _lyrics = []);
    try {
      final q = Uri.encodeComponent('$title $artist');
      final res = await http.get(Uri.parse('https://lrclib.net/api/search?q=$q'));
      if (res.statusCode == 200) {
        final List list = jsonDecode(res.body);
        final match = list.firstWhere(
          (item) => item['syncedLyrics'] != null,
          orElse: () => null,
        );
        if (match != null) {
          final raw = match['syncedLyrics'] as String;
          final parsed = <LyricLine>[];
          final reg = RegExp(r'\[(\d+):(\d+\.\d+)\](.*)');
          for (final line in raw.split('\n')) {
            final m = reg.firstMatch(line);
            if (m != null) {
              final min = int.parse(m.group(1)!);
              final sec = double.parse(m.group(2)!);
              final txt = m.group(3)!.trim();
              if (txt.isNotEmpty) {
                parsed.add(LyricLine(
                  Duration(milliseconds: ((min * 60 + sec) * 1000).round()),
                  txt,
                ));
              }
            }
          }
          setState(() => _lyrics = parsed);
        }
      }
    } catch (_) {}
  }

  void _startSmartRadio() {
    if (_currentTrack == null) return;
    final seed = _currentTrack!;
    final sorted = List<Track>.from(_catalogue)
      ..sort((a, b) {
        final scoreA = (a.artist == seed.artist ? 3 : 0) + (a.genre == seed.genre ? 2 : 0);
        final scoreB = (b.artist == seed.artist ? 3 : 0) + (b.genre == seed.genre ? 2 : 0);
        return scoreB.compareTo(scoreA);
      });
    setState(() {
      _queue = sorted;
    });
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Smart Radio started based on "${seed.title}"'),
        backgroundColor: Colors.white24,
      ),
    );
  }

  void _skipNext() {
    if (_currentTrack == null || _queue.isEmpty) return;
    final idx = _queue.indexWhere((t) => t.msgId == _currentTrack!.msgId);
    if (idx != -1 && idx + 1 < _queue.length) {
      _playTrack(_queue[idx + 1]);
    }
  }

  void _skipPrev() {
    if (_currentTrack == null || _queue.isEmpty) return;
    final idx = _queue.indexWhere((t) => t.msgId == _currentTrack!.msgId);
    if (idx > 0) {
      _playTrack(_queue[idx - 1]);
    }
  }

  @override
  void dispose() {
    _player.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final filtered = _catalogue.where((t) {
      final q = _searchQuery.toLowerCase();
      final matchesText = t.title.toLowerCase().contains(q) ||
          t.artist.toLowerCase().contains(q) ||
          t.album.toLowerCase().contains(q);
      if (_selectedTab == 2) return matchesText && t.bitDepth >= 24;
      return matchesText;
    }).toList();

    return Scaffold(
      body: Stack(
        children: [
          // 1. Dynamic Liquid Mesh Background
          AnimatedContainer(
            duration: const Duration(milliseconds: 900),
            decoration: BoxDecoration(
              gradient: RadialGradient(
                center: const Alignment(-0.4, -0.6),
                radius: 1.4,
                colors: [
                  _dominantColor.withOpacity(0.55),
                  _secondaryColor.withOpacity(0.85),
                  const Color(0xFF07080C),
                ],
              ),
            ),
          ),

          // 2. Frosted Glass Foreground Shell
          SafeArea(
            child: Column(
              children: [
                _buildTopGlassBar(),
                Expanded(
                  child: _selectedTab == 3
                      ? _buildLyricsView()
                      : _selectedTab == 4
                          ? _buildHardwareEqView()
                          : _selectedTab == 1
                              ? _buildAlbumsGrid(filtered)
                              : _buildSongList(filtered),
                ),
                if (_currentTrack != null) _buildMiniGlassPlayer(),
                _buildBottomGlassNav(),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildTopGlassBar() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
      child: Row(
        children: [
          const Text(
            '◈ Aether',
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.5),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(99),
              child: BackdropFilter(
                filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
                child: TextField(
                  onChanged: (v) => setState(() => _searchQuery = v),
                  style: const TextStyle(fontSize: 13),
                  decoration: InputDecoration(
                    hintText: 'Search 16/24-bit FLACs, albums...',
                    prefixIcon: const Icon(Icons.search, size: 18),
                    filled: true,
                    fillColor: Colors.white.withOpacity(0.08),
                    contentPadding: const EdgeInsets.symmetric(vertical: 0),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(99),
                      borderSide: BorderSide.none,
                    ),
                  ),
                ),
              ),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.sync_rounded),
            tooltip: 'Sync Telegram Vault',
            onPressed: () async {
              await http.post(Uri.parse('$_serverUrl/api/sync'));
              _fetchCatalogue();
            },
          ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            onPressed: _showServerSetupDialog,
          ),
        ],
      ),
    );
  }

  Widget _buildSongList(List<Track> tracks) {
    if (tracks.isEmpty) {
      return Center(
        child: Text(
          'Tap ⚙ Settings to connect your Telegram Lossless Vault\nor tap ↻ Sync to load your 16-bit / 24-bit FLAC catalogue.',
          textAlign: TextAlign.center,
          style: TextStyle(color: Colors.white.withOpacity(0.6)),
        ),
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
      itemCount: tracks.length,
      itemBuilder: (context, idx) {
        final t = tracks[idx];
        final isPlaying = _currentTrack?.msgId == t.msgId;
        return Container(
          margin: const EdgeInsets.only(bottom: 8),
          decoration: BoxDecoration(
            color: isPlaying ? Colors.white.withOpacity(0.15) : Colors.white.withOpacity(0.05),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: Colors.white.withOpacity(0.1)),
          ),
          child: ListTile(
            onTap: () => _playTrack(t),
            leading: ClipRRect(
              borderRadius: BorderRadius.circular(10),
              child: CachedNetworkImage(
                imageUrl: '$_serverUrl/api/cover/${t.msgId}',
                width: 48,
                height: 48,
                fit: BoxFit.cover,
                errorWidget: (_, __, ___) => Container(
                  width: 48,
                  height: 48,
                  color: Colors.white12,
                  child: const Icon(Icons.music_note),
                ),
              ),
            ),
            title: Text(
              t.title,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
            ),
            subtitle: Text(
              '${t.artist} • ${t.album}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 12, color: Colors.white.withOpacity(0.65)),
            ),
            trailing: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                color: const Color(0xFFFFD60A).withOpacity(0.16),
                borderRadius: BorderRadius.circular(6),
                border: Border.all(color: const Color(0xFFFFD60A).withOpacity(0.4)),
              ),
              child: Text(
                t.hiResBadge,
                style: const TextStyle(
                  fontSize: 9,
                  fontWeight: FontWeight.w800,
                  color: Color(0xFFFFD60A),
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  Widget _buildAlbumsGrid(List<Track> tracks) {
    final Map<String, Track> albums = {};
    for (final t in tracks) {
      albums.putIfAbsent(t.album, () => t);
    }
    final albumList = albums.values.toList();
    return GridView.builder(
      padding: const EdgeInsets.all(16),
      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: 2,
        childAspectRatio: 0.78,
        crossAxisSpacing: 14,
        mainAxisSpacing: 14,
      ),
      itemCount: albumList.length,
      itemBuilder: (context, idx) {
        final a = albumList[idx];
        return GestureDetector(
          onTap: () {
            setState(() {
              _searchQuery = a.album;
              _selectedTab = 0;
            });
          },
          child: Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: Colors.white.withOpacity(0.06),
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: Colors.white.withOpacity(0.12)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(14),
                    child: CachedNetworkImage(
                      imageUrl: '$_serverUrl/api/cover/${a.msgId}',
                      width: double.infinity,
                      fit: BoxFit.cover,
                      errorWidget: (_, __, ___) => Container(
                        color: Colors.white10,
                        child: const Center(child: Icon(Icons.album, size: 42)),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  a.album,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13),
                ),
                Text(
                  a.artist,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 11, color: Colors.white.withOpacity(0.6)),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildLyricsView() {
    if (_lyrics.isEmpty) {
      return const Center(
        child: Text('Play a track to load time-synced Apple Music-style lyrics.'),
      );
    }
    return StreamBuilder<Duration>(
      stream: _player.positionStream,
      builder: (context, snap) {
        final pos = snap.data ?? Duration.zero;
        int activeIdx = -1;
        for (int i = 0; i < _lyrics.length; i++) {
          if (pos >= _lyrics[i].time) activeIdx = i;
        }
        return ListView.builder(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 30),
          itemCount: _lyrics.length,
          itemBuilder: (context, i) {
            final active = i == activeIdx;
            return GestureDetector(
              onTap: () => _player.seek(_lyrics[i].time),
              child: AnimatedDefaultTextStyle(
                duration: const Duration(milliseconds: 250),
                style: TextStyle(
                  fontSize: active ? 22 : 16,
                  fontWeight: active ? FontWeight.w800 : FontWeight.w600,
                  color: active ? Colors.white : Colors.white.withOpacity(0.32),
                  shadows: active
                      ? [const Shadow(color: Colors.white54, blurRadius: 18)]
                      : null,
                ),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 10),
                  child: Text(_lyrics[i].text),
                ),
              ),
            );
          },
        );
      },
    );
  }

  Widget _buildHardwareEqView() {
    return ListView(
      padding: const EdgeInsets.all(18),
      children: [
        SwitchListTile(
          title: const Text(
            'Bit-Perfect Direct Stream (Bypass DSP)',
            style: TextStyle(fontWeight: FontWeight.bold),
          ),
          subtitle: const Text(
            'Streams pure untouched 16-bit/24-bit FLAC directly to your Android DAC.',
          ),
          value: _bitPerfectMode,
          onChanged: (val) async {
            setState(() => _bitPerfectMode = val);
            await _equalizer.setEnabled(!val);
          },
        ),
        SwitchListTile(
          title: const Text(
            'Spatial Soundstage & Gain Enhancer',
            style: TextStyle(fontWeight: FontWeight.bold),
          ),
          subtitle: const Text('Expands stereo depth using Android hardware DSP.'),
          value: _spatialEnabled,
          onChanged: (val) async {
            setState(() => _spatialEnabled = val);
            await _spatialEnhancer.setEnabled(val);
            if (val) await _spatialEnhancer.setTargetGain(1.5);
          },
        ),
        const Divider(height: 28),
        FutureBuilder<AndroidEqualizerParameters>(
          future: _equalizer.parameters,
          builder: (context, snapshot) {
            final params = snapshot.data;
            if (params == null) {
              return const Center(
                child: Text('Play a lossless song first to initialize Android Hardware EQ bands.'),
              );
            }
            return Column(
              children: params.bands.map((band) {
                return StreamBuilder<double>(
                  stream: band.gainStream,
                  builder: (context, gainSnap) {
                    final gain = gainSnap.data ?? band.gain;
                    return Row(
                      children: [
                        SizedBox(
                          width: 75,
                          child: Text('${band.centerFrequency.round()} Hz'),
                        ),
                        Expanded(
                          child: Slider(
                            min: params.minDecibels,
                            max: params.maxDecibels,
                            value: gain,
                            onChanged: _bitPerfectMode ? null : band.setGain,
                          ),
                        ),
                      ],
                    );
                  },
                );
              }).toList(),
            );
          },
        ),
      ],
    );
  }

  Widget _buildMiniGlassPlayer() {
    final t = _currentTrack!;
    return GestureDetector(
      onTap: _openFullScreenPlayer,
      child: Container(
        margin: const EdgeInsets.fromLTRB(12, 4, 12, 8),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: Colors.white.withOpacity(0.12),
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: Colors.white.withOpacity(0.22)),
        ),
        child: Row(
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: CachedNetworkImage(
                imageUrl: '$_serverUrl/api/cover/${t.msgId}',
                width: 46,
                height: 46,
                fit: BoxFit.cover,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    t.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                  ),
                  Text(
                    '${t.artist} • ${t.hiResBadge}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 11, color: Color(0xFFFFD60A)),
                  ),
                ],
              ),
            ),
            IconButton(
              icon: const Icon(Icons.radio_rounded, size: 20),
              tooltip: 'Start Smart Radio',
              onPressed: _startSmartRadio,
            ),
            StreamBuilder<bool>(
              stream: _player.playingStream,
              builder: (context, snap) {
                final playing = snap.data ?? false;
                return IconButton(
                  icon: Icon(playing ? Icons.pause_circle_filled : Icons.play_circle_fill, size: 34),
                  onPressed: () => playing ? _player.pause() : _player.play(),
                );
              },
            ),
            IconButton(
              icon: const Icon(Icons.skip_next_rounded),
              onPressed: _skipNext,
            ),
          ],
        ),
      ),
    );
  }

  void _openFullScreenPlayer() {
    if (_currentTrack == null) return;
    final t = _currentTrack!;
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (context) {
        return BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 45, sigmaY: 45),
          child: Container(
            height: MediaQuery.of(context).size.height * 0.9,
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: _dominantColor.withOpacity(0.28),
              borderRadius: const BorderRadius.vertical(top: Radius.circular(36)),
              border: Border.all(color: Colors.white24),
            ),
            child: Column(
              children: [
                Container(
                  width: 44,
                  height: 5,
                  decoration: BoxDecoration(
                    color: Colors.white38,
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
                const SizedBox(height: 28),
                ClipRRect(
                  borderRadius: BorderRadius.circular(26),
                  child: CachedNetworkImage(
                    imageUrl: '$_serverUrl/api/cover/${t.msgId}',
                    width: 280,
                    height: 280,
                    fit: BoxFit.cover,
                  ),
                ),
                const SizedBox(height: 20),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                  decoration: BoxDecoration(
                    color: const Color(0xFFFFD60A).withOpacity(0.2),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    t.hiResBadge,
                    style: const TextStyle(
                      color: Color(0xFFFFD60A),
                      fontWeight: FontWeight.w800,
                      fontSize: 11,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  t.title,
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w800),
                ),
                const SizedBox(height: 4),
                Text(
                  '${t.artist} — ${t.album}',
                  style: TextStyle(fontSize: 14, color: Colors.white.withOpacity(0.7)),
                ),
                const SizedBox(height: 20),
                StreamBuilder<Duration>(
                  stream: _player.positionStream,
                  builder: (context, snap) {
                    final pos = snap.data ?? Duration.zero;
                    final dur = _player.duration ?? Duration(seconds: t.duration.round());
                    final maxSec = dur.inSeconds > 0 ? dur.inSeconds.toDouble() : 1.0;
                    return Column(
                      children: [
                        Slider(
                          value: pos.inSeconds.toDouble().clamp(0, maxSec),
                          max: maxSec,
                          onChanged: (v) => _player.seek(Duration(seconds: v.round())),
                        ),
                        Row(
                          mainAxisAlignment: MainAxisAlignment.spaceBetween,
                          children: [
                            Text('${pos.inMinutes}:${(pos.inSeconds % 60).toString().padLeft(2, '0')}'),
                            Text('${dur.inMinutes}:${(dur.inSeconds % 60).toString().padLeft(2, '0')}'),
                          ],
                        ),
                      ],
                    );
                  },
                ),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.skip_previous_rounded, size: 40),
                      onPressed: () {
                        Navigator.pop(context);
                        _skipPrev();
                      },
                    ),
                    StreamBuilder<bool>(
                      stream: _player.playingStream,
                      builder: (context, snap) {
                        final playing = snap.data ?? false;
                        return IconButton(
                          icon: Icon(
                            playing ? Icons.pause_circle_filled : Icons.play_circle_fill,
                            size: 68,
                          ),
                          onPressed: () => playing ? _player.pause() : _player.play(),
                        );
                      },
                    ),
                    IconButton(
                      icon: const Icon(Icons.skip_next_rounded, size: 40),
                      onPressed: () {
                        Navigator.pop(context);
                        _skipNext();
                      },
                    ),
                  ],
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildBottomGlassNav() {
    return ClipRRect(
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 25, sigmaY: 25),
        child: BottomNavigationBar(
          currentIndex: _selectedTab,
          onTap: (i) => setState(() => _selectedTab = i),
          backgroundColor: Colors.white.withOpacity(0.06),
          type: BottomNavigationBarType.fixed,
          selectedItemColor: const Color(0xFFFFD60A),
          unselectedItemColor: Colors.white60,
          items: const [
            BottomNavigationBarItem(icon: Icon(Icons.library_music), label: 'Songs'),
            BottomNavigationBarItem(icon: Icon(Icons.album), label: 'Albums'),
            BottomNavigationBarItem(icon: Icon(Icons.high_quality), label: '24-Bit'),
            BottomNavigationBarItem(icon: Icon(Icons.lyrics), label: 'Lyrics'),
            BottomNavigationBarItem(icon: Icon(Icons.graphic_eq), label: 'EQ & DSP'),
          ],
        ),
      ),
    );
  }

  void _showServerSetupDialog() {
    final ctrl = TextEditingController(text: _serverUrl);
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Telegram Bridge Server URL'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              'Enter the URL of your Telegram Lossless Bridge (Termux localhost or free HuggingFace / Render cloud URL):',
              style: TextStyle(fontSize: 12),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: ctrl,
              decoration: const InputDecoration(hintText: 'http://127.0.0.1:7860'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            onPressed: () async {
              final prefs = await SharedPreferences.getInstance();
              await prefs.setString('server_url', ctrl.text.trim());
              setState(() => _serverUrl = ctrl.text.trim());
              if (context.mounted) Navigator.pop(context);
              _fetchCatalogue();
            },
            child: const Text('Save & Connect'),
          ),
        ],
      ),
    );
  }
}
